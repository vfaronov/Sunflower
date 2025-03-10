from __future__ import absolute_import

import os
import re
import time
import sys
import fnmatch

from gi.repository import GObject, Gtk, Gdk, GLib, Gio
from threading import Thread, Event

from .column_editor import FileList_ColumnEditor

from sunflower import common
from sunflower.gui.input_dialog import ApplicationSelectDialog
from sunflower.gui.input_dialog import CopyDialog, MoveDialog, RenameDialog, DeleteDialog
from sunflower.gui.input_dialog import FileCreateDialog, DirectoryCreateDialog, LinkDialog
from sunflower.gui.properties_window import PropertiesWindow
from sunflower.operation import DeleteOperation, CopyOperation, MoveOperation
from sunflower.parameters import Parameters
from sunflower.plugin_base.item_list import ItemList
from sunflower.plugin_base.monitor import MonitorSignals, MonitorError
from sunflower.plugin_base.provider import FileType, Mode as FileMode, Support as ProviderSupport
from sunflower.widgets.thumbnail_view import ThumbnailView
from sunflower.widgets.emblems_renderer import CellRendererEmblems


class Column:
	NAME = 0
	FORMATED_NAME = 1
	EXTENSION = 2
	SIZE = 3
	FORMATED_SIZE = 4
	MODE = 5
	FORMATED_MODE = 6
	TIME = 7
	FORMATED_TIME = 8
	IS_DIR = 9
	IS_PARENT_DIR = 10
	IS_LINK = 11
	COLOR = 12
	ICON = 13
	SELECTED = 14
	USER_ID = 15
	GROUP_ID = 16
	EMBLEMS = 17
	SORT_DATA = 18


class FileList(ItemList):
	"""General file list plugin

	This plugin was written with various usages in mind. If you need to write
	plugin that will handle files it is strongly suggested that you inherit this class
	and make your own content provider.

	"""
	column_editor = None
	number_split = re.compile('([0-9]+)')

	def __init__(self, parent, notebook, options):
		ItemList.__init__(self, parent, notebook, options)

		self.scheme = 'file'
		self.path = self._options.get('path', os.path.expanduser('~'))
		self._sort_column = self._options.get('sort_column', 0)
		self._sort_ascending = self._options.get('sort_ascending', True)

		section = self._parent.options.section('item_list')
		self._always_visible_items = section.get('always_visible')

		# event object controlling path change thread
		self._thread_active = Event()
		self._main_thread_lock = Event()

		self._item_queue = []
		self._emblem_cache = {}

		# storage system for list items
		self._store = Gtk.TreeStore(
								# name is a string, but it can contain surrogates,
								# so it can't be marshalled as a gstring.
								GObject.TYPE_PYOBJECT,	# Column.NAME
								str,	# Column.FORMATED_NAME
								str,	# Column.EXTENSION
								float,	# Column.SIZE
								str,	# Column.FORMATED_SIZE
								int,	# Column.MODE
								str,	# Column.FORMATED_MODE
								int,	# Column.DATE
								str,	# Column.FORMATED_DATE
								bool,	# Column.IS_DIR
								bool,	# Column.IS_PARENT_DIR
								bool,	# Column.IS_LINK
								str,	# Column.COLOR
								str,	# Column.ICON
								bool,	# Column.SELECTED
								int,	# Column.USER_ID
								int,	# Column.GROUP_ID
								GObject.TYPE_PYOBJECT,	# Column.EMBLEMS
								str		# Column.SORT_DATA
							)

		# set item list model
		self._item_list.set_model(self._store)

		# create columns
		cell_selected = Gtk.CellRendererText()
		cell_icon = Gtk.CellRendererPixbuf()
		cell_name = Gtk.CellRendererText()
		cell_extension = Gtk.CellRendererText()
		cell_size = Gtk.CellRendererText()
		cell_mode = Gtk.CellRendererText()
		cell_date = Gtk.CellRendererText()
		cell_emblems = CellRendererEmblems()

		cell_name.set_property('single-paragraph-mode', True)
		cell_extension.set_property('single-paragraph-mode', True)

		# cell_selected.set_property('width', 30)  # leave enough room for various characters
		cell_selected.set_property('xalign', 1)
		cell_size.set_property('xalign', 1)

		# get default font size
		self._default_column_font_size = {
								'extension': 8,
								'size': 8,
								'mode': 8,
								'date': 8
							}
		self._monospace_renderers = (cell_mode, cell_date)

		# create columns
		col_name = Gtk.TreeViewColumn(_('Name'))
		col_extension = Gtk.TreeViewColumn(_('Ext'))
		col_size = Gtk.TreeViewColumn(_('Size'))
		col_mode = Gtk.TreeViewColumn(_('Mode'))
		col_date = Gtk.TreeViewColumn(_('Date'))

		# set column names
		col_name.name = 'name'
		col_extension.name = 'extension'
		col_size.name = 'size'
		col_mode.name = 'mode'
		col_date.name = 'date'

		# add cell renderer to columns
		col_name.pack_start(cell_icon, False)
		col_name.pack_start(cell_name, True)
		col_name.pack_start(cell_selected, False)
		col_name.pack_start(cell_emblems, False)
		col_extension.pack_start(cell_extension, True)
		col_size.pack_start(cell_size, True)
		col_mode.pack_start(cell_mode, True)
		col_date.pack_start(cell_date, True)

		col_name.add_attribute(cell_name, 'foreground', Column.COLOR)
		col_name.add_attribute(cell_selected, 'foreground', Column.COLOR)
		col_name.set_cell_data_func(cell_selected, self._selected_data_func)
		col_extension.add_attribute(cell_extension, 'foreground', Column.COLOR)
		col_size.add_attribute(cell_size, 'foreground', Column.COLOR)
		col_mode.add_attribute(cell_mode, 'foreground', Column.COLOR)
		col_date.add_attribute(cell_date, 'foreground', Column.COLOR)

		col_name.add_attribute(cell_icon, 'icon-name', Column.ICON)
		col_name.add_attribute(cell_emblems, 'emblems', Column.EMBLEMS)
		col_name.add_attribute(cell_emblems, 'is-link', Column.IS_LINK)
		col_name.add_attribute(cell_name, 'text', Column.FORMATED_NAME)
		col_extension.add_attribute(cell_extension, 'text', Column.EXTENSION)
		col_size.add_attribute(cell_size, 'text', Column.FORMATED_SIZE)
		col_mode.add_attribute(cell_mode, 'text', Column.FORMATED_MODE)
		col_date.add_attribute(cell_date, 'text', Column.FORMATED_TIME)

		col_name.set_resizable(True)
		col_name.set_sizing(Gtk.TreeViewColumnSizing.FIXED)

		col_extension.set_resizable(True)
		col_extension.set_sizing(Gtk.TreeViewColumnSizing.FIXED)

		col_size.set_resizable(True)
		col_size.set_alignment(1)
		col_size.set_sizing(Gtk.TreeViewColumnSizing.FIXED)

		col_mode.set_resizable(True)
		col_mode.set_sizing(Gtk.TreeViewColumnSizing.FIXED)

		col_date.set_resizable(True)
		col_date.set_sizing(Gtk.TreeViewColumnSizing.FIXED)

		# register columns
		self._columns = [col_name, col_extension, col_size, col_mode, col_date]

		# create column editor if needed
		if self.column_editor is None:
			self.__class__.column_editor = FileList_ColumnEditor(self, parent.plugin_options)
			parent.register_column_editor_extension(self.column_editor)

		# set default column sizes for file list
		self._columns_size = (200, 50, 70, 50, 90)
		self._create_default_column_sizes()

		# create a list of columns
		column_sort_data = {
					Column.NAME: col_name,
					Column.EXTENSION: col_extension,
					Column.SIZE: col_size,
					Column.MODE: col_mode,
					Column.TIME: col_date,
				}

		# block columns-changed signal while adding columns
		self._item_list.handler_block_by_func(self._column_changed)

		# configure and pack columns
		for sort_data, column in column_sort_data.items():
			# connect events
			column.connect('clicked', self._set_sort_function, sort_data)
			column.connect('notify::width', self._column_resized)
			column.set_reorderable(True)

			# add to the list
			self._item_list.append_column(column)

		# set tooltip on name column
		self._item_list.set_tooltip_column(Column.NAME)

		# create extension columns
		class_list = self._parent.get_column_extension_classes(self.__class__)

		for ExtensionClass in class_list:
			extension = ExtensionClass(self, self._store)
			column = extension.get_column()

			if column is not None:
				sort_data = extension.get_sort_column()

				# configure column
				column.set_reorderable(True)
				column.set_resizable(True)
				column.set_sizing(Gtk.TreeViewColumnSizing.FIXED)

				# connect signals
				column.connect('notify::width', self._column_resized)
				column.connect('clicked', self._set_sort_function, sort_data)

				# add new column to lists for proper handling
				column_sort_data[sort_data] = column
				self._columns.append(column)
				self._item_list.append_column(column)

		# restore column properties
		self._resize_columns(self._columns)
		self._set_font_size(self._columns)
		self._reorder_columns()

		# release signal block
		self._item_list.handler_unblock_by_func(self._column_changed)

		# set list behavior
		self._item_list.set_headers_clickable(True)
		self._item_list.set_enable_search(True)
		self._item_list.set_search_column(Column.NAME)

		# set row hinting
		section = self._parent.options.section('item_list')
		row_hinting = section.get('row_hinting')
		self._item_list.set_rules_hint(row_hinting)

		# set visibility of tree expanders
		self._show_expanders = section.get('show_expanders')
		self._item_list.set_show_expanders(self._show_expanders)

		# set grid lines
		grid_lines = (
					Gtk.TreeViewGridLines.NONE,
					Gtk.TreeViewGridLines.HORIZONTAL,
					Gtk.TreeViewGridLines.VERTICAL,
					Gtk.TreeViewGridLines.BOTH,
				)[self._parent.options.section('item_list').get('grid_lines')]
		self._item_list.set_grid_lines(grid_lines)

		# set sort function
		if self._sort_column is None \
		or self._sort_column not in column_sort_data:
			# default sort by name
			self._sort_column = Column.NAME
			self._sort_ascending = True

		self._sort_column_widget = column_sort_data[self._sort_column]
		self._apply_sort_function()

		# thumbnail view
		self._thumbnail_view = ThumbnailView(self)
		self._enable_media_preview = self._parent.options.get('media_preview')

		# variable that is used to set focus on newly created files and dirs
		self._item_to_focus = None

		# cache configuration locally
		self._time_format = self._parent.options.section('item_list').get('time_format')
		self._mode_format = self._parent.options.section('item_list').get('mode_format')

		plugin_options = self._parent.plugin_options
		if plugin_options.has_section(self._name) \
		and plugin_options.section(self._name).has('columns'):
			self._show_full_name = 'extension' not in plugin_options.section(self._name).get('columns')

		else:
			self._show_full_name = False

		# change to initial path
		try:
			self.change_path(self.path)

		except:
			# fail-safe jump to user home directory
			self.change_path(os.path.expanduser('~'))

	def set_default_font_size(self, column_name, size):
		"""Set default column font size."""
		self._default_column_font_size.update({column_name: size})

	def _set_font_size(self, columns):
		"""Apply font size from settings."""
		options = self._parent.plugin_options.section(self._name)

		font = common.get_monospace_font_string()

		for column in columns:
			column.set_sizing(Gtk.TreeViewColumnSizing.AUTOSIZE)
			font_size = options.get('font_size_{0}'.format(column.name)) or \
				self._default_column_font_size.get(column.name, None)

			# no font size was specified, skip column
			if font_size is None:
				column.set_sizing(Gtk.TreeViewColumnSizing.FIXED)
				column.set_resizable(True)
				continue

			# apply font size to all cell renderers
			for cell_renderer in column.get_cells():
				try:
					cell_renderer.set_property('size-points', font_size)
					if cell_renderer in self._monospace_renderers:
						cell_renderer.set_property('family', font)

				except TypeError:
					pass

			column.set_sizing(Gtk.TreeViewColumnSizing.FIXED)
			column.set_resizable(True)

	def _control_got_focus(self, widget, data=None):
		"""Handle control gaining focus"""
		ItemList._control_got_focus(self, widget, data)

		if self._enable_media_preview:
			self._handle_cursor_change()

	def _control_lost_focus(self, widget, data=None):
		"""Handle control loosing focus"""
		ItemList._control_lost_focus(self, widget, data)

		if self._enable_media_preview:
			self._thumbnail_view.hide()

	def _handle_cursor_change(self, widget=None, data=None):
		"""Handle cursor change"""
		if not self._enable_media_preview or not self._item_list.has_focus():
			return

		selection = self._item_list.get_selection()
		item_list, selected_iter = selection.get_selected()

		# we need selection for this
		if selected_iter is None:
			return

		is_dir = self._store.get_value(selected_iter, Column.IS_DIR)
		is_parent = item_list.get_value(selected_iter, Column.IS_PARENT_DIR)

		if is_dir or is_parent:
			return

		# create URI from item name and protocol
		file_name = self._get_selection(relative=False)
		protocol = self.get_provider().protocol
		uri = '{0}://{1}'.format(protocol, file_name)

		# show preview if thumbnail exists
		if self._thumbnail_view.can_have_thumbnail(uri):
			# calculate position for preview
			path = self._store.get_path(selected_iter)
			column = self._item_list.get_column(0)
			position = self._item_list.get_cell_area(path, column)
			position.width = self._item_list.get_allocated_width()
			position.x, position.y = self._item_list.convert_tree_to_widget_coords(position.x, position.y)

			# show preivew in specified location
			self._thumbnail_view.show_thumbnail(uri, widget, position)

		else:
			# hide preview if item thumbnail is not available
			self._thumbnail_view.hide()

		return True

	def _handle_tab_close(self):
		"""Handle tab closing"""
		ItemList._handle_tab_close(self)

		# cancel current directory monitor
		self.cancel_monitors()

		# cancel disk usage calculations
		self._parent.disk_usage.cancel_all_for_object(self)

	def _handle_emblem_toggle(self, widget, emblem=None):
		"""Handle toggling emblem for selected item."""
		selection = self._get_selection(relative=True, files_only=False)
		path = self._options.get('path')
		absolute_path = os.path.join(self._options.get('path'), selection)

		# make sure we have emblem specified
		if emblem is None:
			return

		# toggle emblem
		item_path, item_name = os.path.split(absolute_path)
		self._parent.emblem_manager.toggle_emblem(item_path, item_name, emblem)

		# notify monitor about change
		queue = self.get_monitor().get_queue()
		queue.put((MonitorSignals.EMBLEM_CHANGED, os.path.join(path, selection), None))

		return True

	def _execute_selected_item(self, widget=None, data=None):
		"""Execute/Open selected item"""
		selection = self._item_list.get_selection()
		item_list, selected_iter = selection.get_selected()

		# we need selection for this
		if selected_iter is None:
			return

		selected_file = self._get_selection()
		mime_type = self._parent.associations_manager.get_mime_type(path=selected_file)

		is_dir = item_list.get_value(selected_iter, Column.IS_DIR)
		is_parent = item_list.get_value(selected_iter, Column.IS_PARENT_DIR)
		is_archive = self._parent.is_archive_supported(mime_type)

		# preemptively create provider if selected item is archive
		if not is_parent and is_archive and not self.provider_exists(selected_file):
			self.create_provider(selected_file, True)

		if is_dir or is_archive:
			# selected item is directory, we need to change path
			if is_parent:
				# call specialized change path method
				self._parent_directory(widget, data)

			else:
				# just change path
				name = item_list.get_value(selected_iter, Column.NAME)
				self.change_path(os.path.join(self.path, name))

		else:
			# selected item is just a file, execute it
			selected_file = self._get_selection()
			self._parent.associations_manager.execute_file(selected_file, provider=self.get_provider())

		return True  # to prevent command or quick search in single key bindings

	def _execute_with_application(self, widget=None, data=None):
		"""Execute/Open selected item with application user selects from the list"""
		selection = self._get_selection_list()

		if selection is not None and len(selection) > 0:
			dialog = ApplicationSelectDialog(self._parent, selection[0])
			response = dialog.get_response()

			if response[0] == Gtk.ResponseType.OK:
				self._parent.associations_manager.open_file(
														selection,
														exec_command=response[2]
													)

		else:
			# invalid selection, warn user
			dialog = Gtk.MessageDialog(
									self._parent,
									Gtk.DialogFlags.DESTROY_WITH_PARENT,
									Gtk.MessageType.WARNING,
									Gtk.ButtonsType.OK,
									_('Invalid selection!')
								)
			dialog.run()
			dialog.destroy()

		return True

	def _open_in_new_tab(self, widget=None, data=None):
		"""Open selected directory in new tab"""
		selection = self._item_list.get_selection()
		item_list, selected_iter = selection.get_selected()

		name = item_list.get_value(selected_iter, Column.NAME)
		is_dir = item_list.get_value(selected_iter, Column.IS_DIR)

		if is_dir:
			options = Parameters()
			options.set('path', os.path.abspath(os.path.join(self.path, name)))

			self._parent.create_tab(
							self._notebook,
							self.__class__,
							options
						)

		return True

	def _open_directory(self, widget=None, data=None):
		"""Open selected directory"""
		selection = self._item_list.get_selection()
		item_list, selected_iter = selection.get_selected()

		# we need selection for this
		if selected_iter is None:
			return True

		is_dir = item_list.get_value(selected_iter, Column.IS_DIR)
		is_parent = item_list.get_value(selected_iter, Column.IS_PARENT_DIR)

		if is_dir:
			# selected item is directory, we need to change path
			if is_parent:
				# call specialized change path method
				self._parent_directory(widget, data)

			else:
				# just change path
				name = item_list.get_value(selected_iter, Column.NAME)
				self.change_path(os.path.join(self.path, name))

		return True

	def _calculate_disk_usage(self, widget=None, data=None):
		"""Start calculation of disk usage by the selected directory."""
		selection = self._item_list.get_selection()
		item_list, selected_iter = selection.get_selected()

		# we need selection for this
		if selected_iter is None:
			return True

		name = item_list.get_value(selected_iter, Column.NAME)
		is_dir = item_list.get_value(selected_iter, Column.IS_DIR)
		is_parent = item_list.get_value(selected_iter, Column.IS_PARENT_DIR)

		# we can only operate on child directories
		if is_dir and not is_parent:
			monitor = self.get_monitor()

			# start calculation thread
			if self._parent.disk_usage.calculate(
					self,
					monitor.get_queue(),
					self.get_provider(),
					os.path.join(self.path, name)
					):
				# show spinner
				self._title_bar.show_spinner()

		return True

	def _expand_directory(self, widget=None, data=None):
		"""Expand currently selected directory"""
		selection = self._item_list.get_selection()
		item_list, selected_iter = selection.get_selected()

		# we need selection for this
		if selected_iter is None:
			return True

		# get needed data for operation
		name = item_list.get_value(selected_iter, Column.NAME)
		is_dir = item_list.get_value(selected_iter, Column.IS_DIR)
		is_parent = item_list.get_value(selected_iter, Column.IS_PARENT_DIR)

		# don't allow expanding parent directory
		if not is_dir or is_parent:
			return True

		# show expanders if they are hidden
		if not self._show_expanders:
			self._show_expanders = True
			self._item_list.set_show_expanders(True)

		# remove children if directory is already expanded
		if item_list.iter_has_child(selected_iter):
			child = item_list.iter_children(selected_iter)
			while child:
				old_child = child
				child = item_list.iter_next(old_child)
				item_list.remove(old_child)

		# start loader thread and expand directory
		self._load_directory(os.path.join(self.path, name), selected_iter)

		return True

	def _collapse_directory(self, widget=None, data=None):
		"""Collapse currently selected directory"""
		selection = self._item_list.get_selection()
		item_list, selected_iter = selection.get_selected()

		# we need selection for this
		if selected_iter is None:
			return True

		# get parent iter
		if item_list.iter_has_child(selected_iter):
			parent = selected_iter

		else:
			parent = item_list.iter_parent(selected_iter)

		# collapse directory and remove its children
		if parent is not None:
			# collapse row
			self._item_list.collapse_row(item_list.get_path(parent))

			# select parent row
			path = item_list.get_path(parent)
			self._item_list.set_cursor(path)

		return True

	def _create_directory(self, widget=None, data=None):
		"""Prompt user and create directory"""
		dialog = DirectoryCreateDialog(self._parent)
		show_hidden = self._parent.options.section('item_list').get('show_hidden')

		# get response
		response = dialog.get_response()
		mode = dialog.get_mode()

		# create dialog
		if response[0] == Gtk.ResponseType.OK:
			try:
				# set this item to be focused on add
				self._item_to_focus = response[1]

				# try to create directories
				self.get_provider().create_directory(response[1], mode, relative_to=self.path)

				# push monitor event queue
				event_queue = self.get_monitor_queue()
				if event_queue is not None:
					event_queue.put((MonitorSignals.CREATED, response[1], None), False)

			except OSError as error:
				# error creating, report to user
				dialog = Gtk.MessageDialog(
										self._parent,
										Gtk.DialogFlags.DESTROY_WITH_PARENT,
										Gtk.MessageType.ERROR,
										Gtk.ButtonsType.OK,
										_(
											"There was an error creating directory. "
											"Make sure you have enough permissions. "
										) + "\n\n{0}".format(error)
									)
				dialog.run()
				dialog.destroy()

		return True

	def _create_file(self, widget=None, data=None):
		"""Prompt user and create empty file"""
		dialog = FileCreateDialog(self._parent)
		provider = self.get_provider()
		show_hidden = self._parent.options.section('item_list').get('show_hidden')

		# get response
		response = dialog.get_response()
		mode = dialog.get_mode()
		edit_after = dialog.get_edit_file()
		template = dialog.get_template_file()

		if response[0] != Gtk.ResponseType.OK:
			return True  # value denotes handled shortcut

		try:
			# try to create file
			if provider.is_file(os.path.join(self.path, response[1])):
				raise OSError(_("File already exists: {0}").format(response[1]))

			if provider.is_dir(os.path.join(self.path, response[1])):
				raise OSError(_("Directory with same name exists: {0}").format(response[1]))

			# set this item to be focused on add
			self._item_to_focus = response[1]

			# create file
			provider.create_file(response[1], mode=mode, relative_to=self.path)

			# push monitor event queue
			event_queue = self.get_monitor_queue()
			if event_queue is not None:
				event_queue.put((MonitorSignals.CREATED, response[1], None), False)

			# create file from template
			if template is not None:
				with open(template, 'rb') as raw_file:
					data = raw_file.read()

				new_file = provider.get_file_handle(response[1], FileMode.WRITE, relative_to=self.path)
				new_file.truncate()
				new_file.write(data)
				new_file.close()

			# if specified, edit file after creating it
			if edit_after:
				full_path = os.path.join(provider.get_path(), response[1])
				self._parent.associations_manager.edit_file((full_path, ))

		except OSError as error:
			# error creating, report to user
			dialog = Gtk.MessageDialog(
									self._parent,
									Gtk.DialogFlags.DESTROY_WITH_PARENT,
									Gtk.MessageType.ERROR,
									Gtk.ButtonsType.OK,
									_(
										"There was an error creating file. "
										"Make sure you have enough permissions."
									) + "\n\n{0}".format(error)
								)
			dialog.run()
			dialog.destroy()

		return True

	def _create_link(self, widget=None, data=None, original_path=None, hard_link=None):
		"""Create symbolic or hard link"""
		result = False
		if original_path is None:
			provider = self._get_other_provider()
			destintation = self._parent.get_opposite_object(self).path
		else:
			provider = self.get_provider()
			destintation = self.path

		supported_options = provider.get_support()

		if ProviderSupport.SYMBOLIC_LINK in supported_options \
		or ProviderSupport.HARD_LINK in supported_options:
			# configure dialog
			dialog = LinkDialog(self._parent)
			dialog.set_hard_link_supported(ProviderSupport.HARD_LINK in supported_options)

			# set original path in dialog
			if original_path is None:
					original_path = self._get_selection(relative=False)

			dialog.set_original_path(original_path)
			dialog.set_text(os.path.basename(original_path))

			# set hard link dialog option for user
			if hard_link is not None:
				dialog.set_hard_link(hard_link)

			# ask user to confirm linking
			result = dialog.get_response()

			if result[0] == Gtk.ResponseType.OK:
				original_path = result[1]
				link_name = result[2]
				hard_link = result[3]

				try:
					provider.link(
							original_path,
							link_name,
							relative_to=destintation,
							symbolic=not hard_link
						)

				except Exception as error:
					# there was a problem creating link, let the user know
					dialog = Gtk.MessageDialog(
											self._parent,
											Gtk.DialogFlags.DESTROY_WITH_PARENT,
											Gtk.MessageType.ERROR,
											Gtk.ButtonsType.OK,
											_(
												"Error creating new link."
											) +	"\n\n{0}".format(error)
										)
					dialog.run()
					dialog.destroy()

				finally:
					result = True

		else:
			# current file system doesn't support linking
			dialog = Gtk.MessageDialog(
									self._parent,
									Gtk.DialogFlags.DESTROY_WITH_PARENT,
									Gtk.MessageType.INFO,
									Gtk.ButtonsType.OK,
									_('Current file system does not support linking.')
								)
			dialog.run()
			dialog.destroy()

		return result

	def _delete_files(self, widget=None, force_delete=None):
		"""Delete selected files"""
		queue_name = None
		selection = self._get_selection_list(relative=True)

		# return if there is no selection
		if selection is None:
			return

		# check if user has disabled dialog
		show_dialog = self._parent.options.section('confirmations').get('delete_items')
		trash_items = self._parent.options.section('operations').get('trash_files')

		if show_dialog:
			# get context sensitive message
			if force_delete or not trash_items:
				message = ngettext(
						 	'You are about to <i><b>delete</b></i> {0} item.\n'
						 	'Are you sure about this?',
						 	'You are about to <i><b>delete</b></i> {0} items.\n'
						 	'Are you sure about this?',
						 	len(selection)
						 )

			else:
				message = ngettext(
						 	'You are about to move {0} item to <i><b>trash</b></i>.\n'
						 	'Are you sure about this?',
						 	'You are about to move {0} items to <i><b>trash</b></i>.\n'
						 	'Are you sure about this?',
						 	len(selection)
						 )

			# user has confirmation dialog enabled
			dialog = DeleteDialog(self._parent, message.format(len(selection)))
			result, queue_name = dialog.get_response()

			can_continue = result == Gtk.ResponseType.YES

		else:
			# user has confirmation dialog disabled
			can_continue = True

		# if user is sure about removal create operation
		if can_continue:
			operation = DeleteOperation(
									self._parent,
									self.get_provider()
								)

			# set users preference on trashing files
			if force_delete:
				operation.set_force_delete(True)

			# set event queue
			event_queue = self.get_monitor_queue()
			if event_queue is not None:
				operation.set_source_queue(event_queue)

			operation.set_operation_queue(queue_name)
			operation.set_selection(selection)
			operation.start()

		return True

	def _copy_files(self, widget=None, data=None):
		"""Copy selected files"""
		selection = self._get_selection_list(relative=True)

		# return if there is no selection
		if selection is None:
			return

		# get providers
		opposite_object = self._parent.get_opposite_object(self)
		source_provider = self.get_provider()
		destination_provider = None
		destination_monitor = None

		if hasattr(opposite_object, 'get_provider'):
			destination_provider = opposite_object.get_provider()
			destination_monitor = opposite_object.get_monitor()

		# ask confirmation from user
		dialog = CopyDialog(
						self._parent,
						source_provider,
						destination_provider,
						self._get_other_provider().get_path()
					)
		result = dialog.get_response()

		if result[0] == Gtk.ResponseType.OK:
			# if user confirmed copying
			operation = CopyOperation(
									self._parent,
									source_provider,
									destination_provider,
									result[1]  # options from dialog
								)

			# set event queue
			if destination_monitor is not None and destination_monitor.is_manual():
				operation.set_destination_queue(destination_monitor.get_queue())

			# set operation queue
			operation.set_operation_queue(result[2])

			operation.set_selection(selection)
			operation.start()

		return True

	def _move_files(self, widget=None, data=None):
		"""Move selected files"""
		selection = self._get_selection_list(relative=True)

		# return if there is no selection
		if selection is None:
			return

		# get providers
		opposite_object = self._parent.get_opposite_object(self)
		source_provider = self.get_provider()
		destination_provider = None

		if hasattr(opposite_object, 'get_provider'):
			destination_provider = opposite_object.get_provider()

		# ask confirmation from user
		dialog = MoveDialog(
						self._parent,
						source_provider,
						destination_provider,
						self._get_other_provider().get_path()
					)
		result = dialog.get_response()

		if result[0] == Gtk.ResponseType.OK:
			# if user confirmed copying
			operation = MoveOperation(
									self._parent,
									source_provider,
									destination_provider,
									result[1]  # options from dialog
								)

			# set event queues
			source_queue = self.get_monitor_queue()
			if source_queue is not None:
				operation.set_source_queue(source_queue)

			destination_queue = opposite_object.get_monitor_queue()
			if destination_queue is not None:
				operation.set_destination_queue(destination_queue)

			operation.set_selection(selection)
			operation.start()

		return True

	def _rename_file(self, widget=None, data=None):
		"""Rename selected item"""
		selection = self._get_selection()

		# return if there is no selection
		if selection is None:
			return
		is_dir = self.get_provider().is_dir(selection)

		# get base name from selection
		selection = common.decode_file_name(os.path.basename(selection))

		dialog = RenameDialog(self._parent, selection, is_dir)
		result = dialog.get_response()

		if result[0] == Gtk.ResponseType.OK:
			if not self.get_provider().exists(result[1], relative_to=self.path):
				try:
					# rename selected item
					self.get_provider().rename_path(selection, result[1], relative_to=self.path)

					# mark item for selection after rename
					self._item_to_focus = result[1]

				except IOError as error:
					# problem renaming item
					dialog = Gtk.MessageDialog(
											self._parent,
											Gtk.DialogFlags.DESTROY_WITH_PARENT,
											Gtk.MessageType.ERROR,
											Gtk.ButtonsType.OK,
											_(
												"Error renaming specified item. Make sure "
												"you have enough permissions."
											) +	"\n\n{0}".format(error)
										)
					dialog.run()
					dialog.destroy()

			else:
				# file/directory already exists
				dialog = Gtk.MessageDialog(
										self._parent,
										Gtk.DialogFlags.DESTROY_WITH_PARENT,
										Gtk.MessageType.ERROR,
										Gtk.ButtonsType.OK,
										_(
											"File or directory with specified name already "
											"exists in current directory. Item could not "
											"be renamed."
										)
									)
				dialog.run()
				dialog.destroy()

		return True

	def _send_to(self, widget=None, data=None):
		"""Nautilus Send To integration"""
		selection = self._get_selection_list()

		if selection is not None and self.get_provider().is_local:
			params = " ".join(
							'"{0}"'.format(
										os.path.join(self.path, item)
										) for item in selection
							)

			command = "nautilus-sendto {0}&".format(params)
			os.system(command)

	def _item_properties(self, widget=None, data=None):
		"""Show file/directory properties"""
		selection = self._item_list.get_selection()
		item_list, selected_iter = selection.get_selected()

		is_parent = item_list.get_value(selected_iter, Column.IS_PARENT_DIR)

		if not is_parent:
			window = PropertiesWindow(
									self._parent,
									self.get_provider(),
									self._get_selection()
								)

			window.show()

		return True

	def _get_selection(self, relative=False, files_only=False):
		"""Return item with path under cursor"""
		result = None
		selection = self._item_list.get_selection()
		item_list, selected_iter = selection.get_selected()

		if selected_iter:
			is_dir = item_list.get_value(selected_iter, Column.IS_DIR)
			is_parent = item_list.get_value(selected_iter, Column.IS_PARENT_DIR)

			if not is_parent and ((not files_only) or (files_only and not is_dir)):
				item = item_list.get_value(selected_iter, Column.NAME)
				result = item if relative else os.path.join(self.path, item)

		return result

	def _get_selection_list(self, under_cursor=False, relative=False, files_only=False, starting_iter=None):
		"""Return list of selected items

		This list is used by many other methods inside this program,
		including 'open with' handlers, execute_selected file, etc.

		"""
		result = []

		if under_cursor:
			selection = self._get_selection(relative=relative, files_only=files_only)
			if selection is None:
				result = None
			else:
				result.append(self._get_selection())

		else:
			list_iter = starting_iter or self._store.get_iter_first()

			while list_iter:
				is_dir = self._store.get_value(list_iter, Column.IS_DIR)
				is_selected = self._store.get_value(list_iter, Column.SELECTED)
				name = self._store.get_value(list_iter, Column.NAME)

				# only add to the result list if item matches selection
				if is_selected and ((not files_only) or (files_only and not is_dir)):
					result.append(name if relative else os.path.join(self.path, name))

				# if iter has children check them too
				if self._store.iter_has_child(list_iter):
					sublist = self._get_selection_list(
									under_cursor,
									relative,
									files_only,
									self._store.iter_children(list_iter)
								)

					if sublist is not None:
						result.extend(sublist)

				list_iter = self._store.iter_next(list_iter)

			if len(result) == 0 and starting_iter is None:
				selection = self._get_selection(relative=relative, files_only=files_only)
				if selection is None:
					result = None
				else:
					result.append(selection)

		return result

	def _prepare_popup_menu(self):
		"""Populate pop-up menu items"""
		selection = self._item_list.get_selection()
		item_list, selected_iter = selection.get_selected()
		associations_manager = self._parent.associations_manager
		menu_manager = self._parent.menu_manager
		if selected_iter is None:
			cursor_path, focus_column = self._item_list.get_cursor()
			selected_iter = item_list.get_iter(cursor_path)

		is_dir = item_list.get_value(selected_iter, Column.IS_DIR)
		is_parent = item_list.get_value(selected_iter, Column.IS_PARENT_DIR)

		# get selected item
		filename = self._get_selection()
		selection = self._get_selection_list()

		# detect mime type
		if is_dir:
			mime_type = 'inode/directory'

		else:
			mime_type = associations_manager.get_mime_type(filename)

			# try to detect by content
			if associations_manager.is_mime_type_unknown(mime_type):
				data = associations_manager.get_sample_data(filename, self.get_provider())
				mime_type = associations_manager.get_mime_type(data=data)

		# call parent method which removes existing menu items
		ItemList._prepare_popup_menu(self)

		# update additional options menu
		additional_options = menu_manager.get_additional_options_for_type(mime_type, selection, self.get_provider())
		for menu_item in additional_options:
			self._additional_options_menu.append(menu_item)

		# get associated applications
		program_list = menu_manager.get_items_for_type(mime_type, selection)
		custom_list = menu_manager.get_custom_items_for_type(mime_type, selection)

		# create open with menu
		for menu_item in program_list:
			self._open_with_menu.append(menu_item)

		# add separator if there are other menu items
		if len(program_list) > 0:
			separator = Gtk.SeparatorMenuItem()
			separator.show()
			self._open_with_menu.append(separator)

		# add custom menu items if needed
		if len(custom_list) > 0:
			for menu_item in custom_list:
				self._open_with_menu.append(menu_item)

			# add separator if needed
			if len(program_list) > 0:
				separator = Gtk.SeparatorMenuItem()
				separator.show()
				self._open_with_menu.append(separator)

		# create an option for opening selection with custom command
		open_with_other = Gtk.MenuItem(_('Other application...'))
		open_with_other.connect('activate', self._execute_with_application)
		open_with_other.show()

		self._open_with_menu.append(open_with_other)

		# disable/enable items
		self._open_with_item.set_sensitive(not is_parent)
		self._open_new_tab_item.set_visible(is_dir)
		self._additional_options_item.set_sensitive(len(additional_options) > 0)
		self._cut_item.set_sensitive(not is_parent)
		self._copy_item.set_sensitive(not is_parent)
		self._paste_item.set_sensitive(self._parent.is_clipboard_item_list())
		self._send_to_item.set_sensitive(self.get_provider().is_local and not is_parent)
		self._rename_item.set_sensitive(not is_parent)
		self._delete_item.set_sensitive(not is_parent)
		self._properties_item.set_sensitive(not is_parent)

	def _prepare_emblem_menu(self):
		"""Prepare emblem menu."""
		# TODO: Remove. Deprecated.
		emblem_list = self._parent.emblem_manager.get_available_emblems()

		for emblem in emblem_list:
			# create image
			image = Gtk.Image()
			image.set_from_icon_name(emblem, Gtk.IconSize.MENU)

			# create menu item
			menu_item = Gtk.ImageMenuItem(emblem)
			menu_item.set_image(image)
			menu_item.set_always_show_image(True)
			menu_item.connect('activate', self._handle_emblem_toggle, emblem)

			# add emblem to menu
			self._emblem_menu.append(menu_item)

		self._emblem_menu.show_all()

	def _get_popup_menu_position(self, menu=None, *args):
		"""Positions menu properly for given row"""
		selection = self._item_list.get_selection()
		item_list, selected_iter = selection.get_selected()
		if selected_iter is None:
			cursor_path, focus_column = self._item_list.get_cursor()
			selected_iter = item_list.get_iter(cursor_path)

		# grab cell and tree rectangles
		rect = self._item_list.get_cell_area(item_list.get_path(selected_iter), self._columns[0])
		tree_rect = self._item_list.get_visible_rect()

		# relative to tree
		x, y = rect.x, rect.y
		x, y = self._item_list.convert_tree_to_widget_coords(x, y)

		# modify coordinate by tree display rectangle vertical offset
		y += tree_rect.y

		# relative to window
		x, y = self._item_list.translate_coordinates(self._parent, x, y)

		# return calculated coordinates and original cell dimensions
		rect.x, rect.y = x, y
		return rect

	def _set_sort_function(self, widget, data=None):
		"""Set sorting method stored in data

		If no data (sort column) is provided we just reset the sort function
		parameters using predefined column and order.

		"""
		if widget is not self._sort_column_widget:
			self._sort_column_widget = widget

		if data is not None:
			if self._sort_column == data:
				# reverse sorting if column is already sorted
				self._sort_ascending = not self._sort_ascending

			else:
				# set sorting column
				self._sort_column = data

		self._apply_sort_function()

	def _apply_sort_function(self, focus_selected=True):
		"""Apply sort settings"""
		# set sort indicator only on one column
		for column in self._columns:
			selected = column is self._sort_column_widget
			column.set_sort_indicator(selected)

		# apply sorting function
		order = [Gtk.SortType.DESCENDING, Gtk.SortType.ASCENDING][self._sort_ascending]
		self._sort_column_widget.set_sort_order(order)
		self._store.set_sort_column_id(Column.SORT_DATA, order)
		self._generate_sort_data()

	def _clear_sort_function(self):
		"""Clear sort settings"""
		self._store.set_sort_column_id(Gtk.TREE_SORTABLE_UNSORTED_SORT_COLUMN_ID, True)

	def _generate_sort_data(self, parent=None, iters=None):
		"""Generate sort data for all iters in the first level or children of the provided
		parent. Separate `iters` list is added as a convenience to allow regenerating sort
		data for specific items in the list."""
		bool_values = ['1', '0'] if self._sort_ascending else ['0', '1']

		# find starting point
		found_iter = None
		if parent is None:
			found_iter = self._store.get_iter_first()

		elif self._store.iter_has_child(parent):
			found_iter = self._store.iter_children(parent)

		def generate_data(for_iter):
			is_dir = self._store.get_value(for_iter, Column.IS_DIR)
			is_parent = self._store.get_value(for_iter, Column.IS_PARENT_DIR)
			value = self._store.get_value(for_iter, self._sort_column)

			if self._sort_number_sensitive and self._sort_column == Column.NAME:
				value = ''.join([part.rjust(12, '0') if part.isdigit() else part for part in self.number_split.split(value)])

			if isinstance(value, str) and not self._sort_case_sensitive:
				value = value.lower()

			if isinstance(value, int) or isinstance(value, float):
				value = str(value).rjust(12, '0')

			return '{}{}{}'.format(bool_values[is_parent], bool_values[is_dir], value)

		# collect data for all iters
		update_data = []
		while found_iter:
			update_data.append((found_iter, generate_data(found_iter)))
			found_iter = self._store.iter_next(found_iter)

		# process provided list
		if iters:
			for found_iter in iters:
				update_data.append((found_iter, generate_data(found_iter)))
				found_iter = self._store.iter_next(found_iter)

		# delayed data update since we can't read and write at the same time
		for item_iter, sort_data in update_data:
			self._store.set_value(item_iter, Column.SORT_DATA, sort_data)

		# move cursor to previously selected element
		selection = self._item_list.get_selection()
		item_list, iter_to_scroll = selection.get_selected()
		if iter_to_scroll:
			path_to_scroll = item_list.get_path(iter_to_scroll)
			self._item_list.scroll_to_cell(path_to_scroll, None, True, 0.5)

	def _clear_list(self):
		"""Clear item list."""
		self._store.clear()

	def _directory_changed(self, monitor, event, path, other_path, parent=None):
		"""Callback method fired when contents of directory has been changed."""
		show_hidden = self._parent.options.section('item_list').get('show_hidden')

		# make sure we are working with relative paths
		if path.startswith(self.path):
			path = path[len(self.path)+1:]

		# get parent path
		parent_path = None

		if parent is not None:
			# form relative path for easier handling
			parent_path = self._store.get_value(parent, Column.NAME)

		elif parent is None and os.path.sep in path:
			# find parent for fallback monitor
			path_fragments = path.split(os.path.sep)
			parent_path = os.path.dirname(path)
			path = path_fragments[-1]
			path_fragments = path_fragments[:-1]

			while len(path_fragments) > 0:
				fragment = path_fragments.pop(0)
				parent = self._find_iter_by_name(fragment, parent)

		# check for list of always hidden files
		provider = self.get_provider()
		always_hidden = []

		if not show_hidden and provider.exists('.hidden', relative_to=parent_path):
			raw_file = provider.get_file_handle('.hidden', FileMode.READ, relative_to=parent_path)
			always_hidden.extend(raw_file.read().splitlines())
			raw_file.close()

		# override hidden list with always visible items
		always_hidden = [item for item in always_hidden if item not in self._always_visible_items]

		# node created
		should_add = False
		if event is MonitorSignals.CREATED:
			# fix problem with duplicating items when file was saved with GIO
			if self._find_iter_by_name(path, parent) is None:
				should_add = True

			# check for hidden item or backup file
			if should_add \
			and not show_hidden \
			and (path[0] == '.' or path[-1] == '~') \
			and path not in self._always_visible_items:
				should_add = False

			# check if path is in any of the filters
			if should_add and len(always_hidden) > 0:
				should_add = path not in always_hidden

			# add item
			if should_add:
				self._add_item(path, parent, parent_path)
				Gdk.threads_add_idle(GLib.PRIORITY_HIGH_IDLE, self._flush_queue, parent)

			else:
				self._update_item_details_by_name(path, parent, parent_path)

		# node renamed
		elif event is MonitorSignals.MOVED:
			# fix problem with duplicating items when file was saved with GIO
			if self._find_iter_by_name(other_path, parent) is None:
				should_add = True

			# make sure we are working with relative paths
			if other_path.startswith(self.path):
				other_path = other_path[len(self.path) + 1:]

			# check for hidden item or backup file
			if not show_hidden \
			and (other_path[0] == '.' or other_path[-1] == '~') \
			and other_path not in self._always_visible_items:
				should_add = False

			# check if path is in any of the filters
			if should_add and len(always_hidden) > 0:
				should_add = other_path not in always_hidden

			self._delete_item_by_name(path, parent)

			if should_add:
				self._add_item(other_path, parent, parent_path)
				Gdk.threads_add_idle(GLib.PRIORITY_HIGH_IDLE, self._flush_queue, parent)
			else:
				self._update_item_details_by_name(other_path, parent, parent_path)

		# node deleted
		elif event is MonitorSignals.DELETED:
			self._delete_item_by_name(path, parent)

		# node changed
		elif event is MonitorSignals.CHANGED:
			self._update_item_details_by_name(path, parent, parent_path)

		# attributes changes
		elif event is MonitorSignals.ATTRIBUTE_CHANGED:
			self._update_item_attributes_by_name(path, parent, parent_path)

		# emblem changes
		elif event is MonitorSignals.EMBLEM_CHANGED:
			self._update_emblems_by_name(path, parent, parent_path)

		# directory size calculation update
		elif event is MonitorSignals.DIRECTORY_SIZE_CHANGED:
			self._update_directory_size_by_name(path, parent)

		# directory size calculation has finied
		elif event is MonitorSignals.DIRECTORY_SIZE_STOPPED:
			self._title_bar.hide_spinner()

		self._change_title_text()
		self._update_status_with_statistis()

		return True

	def _select_all(self, widget, data=None):
		"""Proxy method for selecting all items"""
		if self._dirs['selected'] < self._dirs['count'] or self._files['selected'] < self._files['count']:
			self.select_all()

		else:
			self._deselect_all(widget, data=None)

		return True

	def _deselect_all(self, widget, data=None):
		"""Proxy method for deselecting all items"""
		self.deselect_all()
		return True

	def _invert_selection(self, widget, data=None):
		"""Proxy method for selecting all items"""
		self.invert_selection()
		return True

	def _toggle_selection(self, widget, data=None, advance=True):
		"""Toggle item selection"""
		selection = self._item_list.get_selection()
		item_list, selected_iter = selection.get_selected()

		is_dir = item_list.get_value(selected_iter, Column.IS_DIR)
		is_parent = item_list.get_value(selected_iter, Column.IS_PARENT_DIR)
		size = item_list.get_value(selected_iter, Column.SIZE)

		if not is_parent:
			# get current status of iter
			selected = not item_list.get_value(selected_iter, Column.SELECTED)
			color = (None, self._selection_color)[selected]

			if is_dir:
				self._dirs['selected'] += [-1, 1][selected]
			else:
				self._files['selected'] += [-1, 1][selected]
				self._size['selected'] += [-1, 1][selected] * size

			item_list.set_value(selected_iter, Column.COLOR, color)
			item_list.set_value(selected_iter, Column.SELECTED, selected)

		# update status bar
		ItemList._toggle_selection(self, widget, data, advance)
		self._update_status_with_statistis()

		if advance:
			# select next item in the list
			next_iter = item_list.iter_next(selected_iter)
			if next_iter is not None:
				# iter is not last in the list
				path = item_list.get_path(next_iter)
				self._item_list.set_cursor(path)

			elif item_list.iter_parent(selected_iter) is not None:
				# if iter is part of expanded directory advance through parent
				next_iter = item_list.iter_next(item_list.iter_parent(selected_iter))

				if next_iter is not None:
					path = item_list.get_path(next_iter)
					self._item_list.set_cursor(path)

		return True

	def _select_range(self, start_path, end_path):
		"""Set items in range to status opposite from first item in selection"""
		if len(self._store) == 1:  # exit when list doesn't have items
			return

		# get current selection
		current_iter = self._store.get_iter(start_path)

		# swap paths if selecting from bottom up
		if start_path[0] > end_path[0]:
			start_path, end_path = end_path, start_path

		# make sure start path is not parent
		if start_path[0] == 0:
			start_path = (1, )

		# values to be set in columns
		selected = not self._store.get_value(current_iter, Column.SELECTED)
		color = (None, self._selection_color)[selected]

		for index in range(start_path[0], end_path[0] + 1):
			current_iter = self._store.get_iter((index, ))

			# get current iter information
			size = self._store.get_value(current_iter, Column.SIZE)
			is_dir = self._store.get_value(current_iter, Column.IS_DIR)
			status = self._store.get_value(current_iter, Column.SELECTED)

			# set selection
			self._store.set_value(current_iter, Column.COLOR, color)
			self._store.set_value(current_iter, Column.SELECTED, selected)

			# modify counters only when status is changed
			if selected is not status:
				if is_dir:
					self._dirs['selected'] += [1, -1][status]
				else:
					self._files['selected'] += [1, -1][status]
					self._size['selected'] += [1, -1][status] * size

		# call parent method
		ItemList._select_range(self, start_path, end_path)

		# update status
		self._update_status_with_statistis()

	def _edit_selected(self, widget=None, data=None):
		"""Abstract method to edit currently selected item"""
		selection_list = self._get_selection_list(relative=False, files_only=True)

		# display error message if selection has no files
		if selection_list is None:
			dialog = Gtk.MessageDialog(
									widget,
									Gtk.DialogFlags.DESTROY_WITH_PARENT,
									Gtk.MessageType.INFO,
									Gtk.ButtonsType.OK,
									_('No files selected.')
								)
			dialog.run()
			dialog.destroy()
			return True

		if len(selection_list) > 0:
			self._parent.associations_manager.edit_file(selection_list)

		return True

	def _selected_data_func(self, column, cell, store, selected_iter, data=None):
		"""Handle setting selected identifier"""
		selected = store.get_value(selected_iter, Column.SELECTED)
		cell.set_property('text', (None, self._selection_indicator)[selected])

	def _find_iter_by_name(self, name, parent=None):
		""" Find and return item by name"""
		result = None
		found_iter = None

		# find starting point
		if parent is None:
			found_iter = self._store.get_iter_first()

		elif self._store.iter_has_child(parent):
			found_iter = self._store.iter_children(parent)
			name = os.path.join(self._store.get_value(parent, Column.NAME), name)

		# check all the iters for specified name
		while found_iter:
			if self._store.get_value(found_iter, Column.NAME) == name:
				result = found_iter
				break

			found_iter = self._store.iter_next(found_iter)

		return result

	def _add_item(self, filename, parent=None, parent_path=None):
		"""Add item to the list"""
		result = None
		provider = self.get_provider()
		full_path = os.path.join(self.path, parent_path) if parent_path else self.path
		is_link = False

		# get file information
		file_stat = provider.get_stat(filename, relative_to=full_path)

		# retrieve real information for special files
		if file_stat.type is FileType.LINK:
			is_link = True
			file_stat = provider.get_stat(filename, relative_to=full_path, follow=True)

		# prepare values
		file_size = file_stat.size
		file_mode = file_stat.mode
		file_date = file_stat.time_modify
		is_dir = file_stat.type is FileType.DIRECTORY

		# directory
		if file_stat.type is FileType.DIRECTORY:
			directory_path = os.path.join(full_path, filename)
			icon = self._parent.icon_manager.get_icon_for_directory(directory_path)

			if parent is None:
				self._dirs['count'] += 1

		# regular file
		elif file_stat.type is FileType.REGULAR:
			icon = self._parent.icon_manager.get_icon_for_file(filename)

			if parent is None:
				self._files['count'] += 1
				self._size['total'] += file_size

		# invalid links or files
		else:
			icon = 'image-missing'

			if parent is None:
				self._files['count'] += 1

		# add item to the list
		try:
			# don't allow extension splitting on directories
			formated_file_mode = common.format_mode(file_mode, self._mode_format)
			formated_file_date = time.strftime(self._time_format, time.localtime(file_date))

			if not is_dir:
				if not self._second_extension:
					# regular extension split
					file_info = os.path.splitext(filename)

				else:
					# split with support for second level of extension
					raw = filename.rsplit('.', 2)
					file_info = (raw, '') if len(raw) == 0 else (raw[0], '.{0}'.format('.'.join(raw[1:])))

				if self._show_full_name:
					file_info = (filename, file_info[1])

				formated_file_size = common.format_size(file_size, self._size_format, False)

			else:
				# item is a directory
				file_info = (filename, '')
				formated_file_size = '<DIR>'

			data = (
					os.path.join(parent_path, filename) if parent_path else filename,
					common.decode_file_name(file_info[0]),
					common.decode_file_name(file_info[1][1:]),
					file_size,
					formated_file_size,
					file_mode,
					formated_file_mode,
					file_date,
					formated_file_date,
					is_dir,
					False,
					is_link,
					None,
					icon,
					None,
					file_stat.user_id,
					file_stat.group_id,
					self._emblem_cache[filename] if filename in self._emblem_cache else None,
					''
				)

			self._item_queue.append(data)

			if len(self._item_queue) == 100:
				Gdk.threads_add_idle(GLib.PRIORITY_HIGH_IDLE, self._flush_queue, parent)

		except Exception as error:
			print(u'Error: {0} - {1}'.format(filename, str(error)))

		return result

	def _flush_queue(self, parent=None):
		"""Add items in queue to the list"""
		queued_iters = []
		path_to_select = None

		# add items from the queue
		for data in self._item_queue:
			new_iter = self._store.append(parent, data)
			queued_iters.append(new_iter)

			# focus specified item
			if self._item_to_focus == data[0]:
				path_to_select = self._store.get_path(new_iter)

		# schedule sort data update
		Gdk.threads_add_idle(GLib.PRIORITY_DEFAULT_IDLE, self._generate_sort_data, None, queued_iters)

		# select path if needed
		if path_to_select is not None:
			Gdk.threads_add_idle(GLib.PRIORITY_HIGH_IDLE, self._item_list.set_cursor, path_to_select)

		# clear item queue
		self._item_queue[:] = []

		# expand row if needed
		if parent is not None:
			self._item_list.expand_row(self._store.get_path(parent), False)

	def _delete_item_by_name(self, name, parent):
		"""Removes item with 'name' from the list"""
		selection = self._item_list.get_selection()
		item_list, selected_iter = selection.get_selected()

		# get currently selected name
		selected_name = None
		if selected_iter is not None:
			selected_name = item_list.get_value(selected_iter, Column.NAME)

		# find iter matching name
		found_iter = self._find_iter_by_name(name, parent)

		if found_iter is not None:
			iter_name = self._store.get_value(found_iter, Column.NAME)

			if item_list.get_value(found_iter, Column.IS_DIR):
				self._dirs['count'] -= 1

				# update selected counters
				if item_list.get_value(found_iter, Column.SELECTED):
					self._dirs['selected'] -= 1

			else:
				self._files['count'] -= 1
				self._size['total'] -= item_list.get_value(found_iter, Column.SIZE)

				# update selected counters
				if item_list.get_value(found_iter, Column.SELECTED):
					self._files['selected'] -= 1
					self._size['selected'] -= item_list.get_value(found_iter, Column.SIZE)

			# remove
			self._store.remove(found_iter)

	def _update_item_details_by_name(self, name, parent, parent_path):
		"""Update item details (size, time, etc.) on changed event"""
		found_iter = self._find_iter_by_name(name, parent)
		provider = self.get_provider()

		if found_iter is not None:
			# get node stats
			is_dir = self._store.get_value(found_iter, Column.IS_DIR)
			path = self.path if parent_path is None else os.path.join(self.path, parent_path)
			file_stat = provider.get_stat(name, relative_to=path)

			file_size = file_stat.size
			file_mode = file_stat.mode
			file_date = file_stat.time_modify

			if not is_dir:
				# format file size
				formated_file_size = common.format_size(file_size, self._size_format, False)

			else:
				# item is a directory
				formated_file_size = '<DIR>'

			formated_file_mode = common.format_mode(file_mode, self._mode_format)
			formated_file_date = time.strftime(self._time_format, time.localtime(file_date))

			# update list store
			self._store.set_value(found_iter, Column.SIZE, file_size)
			self._store.set_value(found_iter, Column.MODE, file_mode)
			self._store.set_value(found_iter, Column.TIME, file_date)
			self._store.set_value(found_iter, Column.FORMATED_SIZE, formated_file_size)
			self._store.set_value(found_iter, Column.FORMATED_MODE, formated_file_mode)
			self._store.set_value(found_iter, Column.FORMATED_TIME, formated_file_date)

			# regenerate sort data
			self._generate_sort_data(iters=[found_iter,])

	def _update_item_attributes_by_name(self, name, parent, parent_path):
		"""Update item attributes column by name"""
		found_iter = self._find_iter_by_name(name, parent)
		provider = self.get_provider()

		if found_iter is not None:
			# get node stats
			path = self.path if parent_path is None else os.path.join(self.path, parent_path)
			file_stat = provider.get_stat(name, relative_to=path)

			file_mode = file_stat.mode
			file_date = file_stat.time_modify
			formated_file_mode = common.format_mode(file_mode, self._mode_format)
			formated_file_date = time.strftime(self._time_format, time.localtime(file_date))

			# update list store
			self._store.set_value(found_iter, Column.MODE, file_mode)
			self._store.set_value(found_iter, Column.TIME, file_date)
			self._store.set_value(found_iter, Column.FORMATED_MODE, formated_file_mode)
			self._store.set_value(found_iter, Column.FORMATED_TIME, formated_file_date)

			# regenerate sort data
			self._generate_sort_data(iters=[found_iter,])

	def _change_title_text(self, text=None):
		"""Change title label text and add free space display"""
		if text is None:
			text = self.path

		# get system information
		system_size = self.get_provider().get_system_size(self.path)

		# format numbers
		size_available = common.format_size(system_size.size_available, self._size_format)
		size_total = common.format_size(system_size.size_total, self._size_format)

		# set title and subtitle
		self._title_bar.set_title(text)
		self._label_free_space.set_text('{2} {0} - {3} {1}'.format(
							size_available,
							size_total,
							_('Free:'),
							_('Total:')
						))
		self._progress_free_space.set_max_value(system_size.size_total)
		self._progress_free_space.set_value(system_size.size_total - system_size.size_available)

	def _drag_motion(self, widget, drag_context, x, y, timestamp):
		"""Handle dragging data over widget"""
		path = None
		action = Gdk.DragAction.DEFAULT

		self._drag_auto_scroll(widget, x, y)

		try:
			# get item under cursor
			path_at_row, position = widget.get_dest_row_at_pos(x, y)
			under_cursor = self._store.get_iter(path_at_row)

			# check if drag destination is a directory
			if self._store.get_value(under_cursor, Column.IS_DIR):
				path = path_at_row
				action = drag_context.get_actions()
			else:
				path = self._store.get_path(self._store.iter_parent(under_cursor))

		except TypeError:
			pass

		widget.set_drag_dest_row(path, Gtk.TreeViewDropPosition.INTO_OR_AFTER)

		return True

	def _drag_auto_scroll(self, widget, x, y):
		"""Automatically scroll while dragging objects."""
		adjustment = widget.get_vadjustment()

		if adjustment is None:
			return

		value = adjustment.get_value()
		upper = adjustment.get_upper()
		lower = adjustment.get_lower()
		step = adjustment.get_step_increment()
		size = adjustment.get_page_size()

		row_height = widget.get_cell_area((0,), widget.get_column(0)).height

		if y < row_height * 2:
			value = value - step if value > lower else lower
		elif y > (widget.get_allocation().height - row_height * 2):
			value = value + step if value < (upper - size) else upper - size

		adjustment.set_value(value)
		adjustment.value_changed()

	def _drag_ask(self):
		"""Show popup menu and return selected action"""
		result = []

		# menu items to offer to user
		actions = (
				{
					'action': Gdk.DragAction.COPY,
					'name': _('Copy here'),
					'icon': 'edit-copy-symbolic'
				},
				{
					'action': Gdk.DragAction.MOVE,
					'name': _('Move here'),
					'icon': 'edit-cut-symbolic'
				},
				{
					'action': Gdk.DragAction.LINK,
					'name': _('Link here'),
					'icon': None
				}
			)

		# create menu
		menu = Gtk.Menu()
		for action in actions:
			menu_item = Gtk.ImageMenuItem()

			if action['icon']:
				image = Gtk.Image()
				image.set_from_icon_name(action['icon'], Gtk.IconSize.MENU)
				menu_item.set_image(image)

			menu_item.set_label(action['name'])
			menu_item.connect(
					'activate',
					lambda widget, selected_action: result.append(selected_action),
					action['action']
				)
			menu.append(menu_item)

		# add separator
		menu.append(Gtk.SeparatorMenuItem())

		# create cancel option
		menu_item = Gtk.ImageMenuItem()
		menu_item.set_label(_('Cancel'))
		menu.append(menu_item)

		# show menu in separate user interface thread
		menu.show_all()
		menu.connect('deactivate', Gtk.main_quit)
		menu.popup_at_pointer()
		Gtk.main()

		return result[0] if result else None

	def _drag_data_received(self, widget, drag_context, x, y, selection_data, info, timestamp):
		"""Handle dropping files on file list"""
		result = False
		action = drag_context.get_selected_action()
		item_list = selection_data.get_data().decode('utf-8').splitlines(False)

		# prepare data for copying
		protocol, path = item_list[0].split('://', 1)

		# handle data
		if action == Gdk.DragAction.ASK:
			action = self._drag_ask()

		if action in (Gdk.DragAction.COPY, Gdk.DragAction.MOVE):
			# handle copy and move operations
			operation = {
						Gdk.DragAction.COPY: 'copy',
						Gdk.DragAction.MOVE: 'move'
					}

			try:
				# get item at cursor
				path, position = widget.get_dest_row_at_pos(x, y)
				destination_iter = self._store.get_iter(path)

				# prepare destination path from selected item
				destination = self._store.get_value(destination_iter, Column.NAME)
				destination = os.path.join(self.path, destination)

				# handle cases when user select parent directory
				if self._store.get_value(destination_iter, Column.IS_PARENT_DIR):
					destination = os.path.dirname(os.path.dirname(destination))

				elif not self._store.get_value(destination_iter, Column.IS_DIR):
					destination =  os.path.dirname(destination)

			except TypeError:
				destination = self.path

			result = self._handle_external_data(
											operation[action],
											protocol,
											item_list,
											destination
										)

		elif action == Gdk.DragAction.LINK:
			# handle linking
			result = self._create_link(original_path=path)

		# notify source application about operation outcome
		drag_context.finish(result, False, timestamp)

	def _drag_data_get(self, widget, drag_context, selection_data, info, time):
		"""Handle data request from destination widget"""
		protocol = self.get_provider().get_protocol()

		selection = []
		for file_name in self._get_selection_list():
			if protocol == 'file':
				file_name = '{0}://{1}'.format(protocol, file_name)
			selection.append(file_name)

		selection_data.set_uris(selection)
		return True

	def _get_supported_drag_types(self):
		"""Return list of supported data for drag'n'drop events"""
		return [
				Gtk.TargetEntry.new('text/uri-list', 0, 0),
			]

	def _get_supported_drag_actions(self):
		"""Return integer representing supported drag'n'drop actions"""
		return Gdk.DragAction.COPY | Gdk.DragAction.MOVE | Gdk.DragAction.ASK | Gdk.DragAction.LINK

	def _load_directory(self, path, parent=None, clear_store=False):
		"""Load directory content into store"""
		# if there is already active thread, stop it
		if self._thread_active.is_set():
			self._main_thread_lock.set()
			self._thread_active.clear()

			while self._main_thread_lock.is_set():
				Gtk.main_iteration_do(blocking=False)

		# disable updates on cursor change
		self._item_list.handler_block_by_func(self._handle_cursor_change)

		# clear list
		if clear_store:
			self._clear_list()

		# clear item queue
		self._item_queue[:] = []

		# default value for parent path
		parent_path = None

		# cache objects and settings
		show_hidden = self._parent.options.section('item_list').get('show_hidden')

		# add parent option for parent directory
		if path != self.get_provider().get_root_path(path):
			if parent is None:
				self._store.append(parent, (
					os.path.pardir, os.path.pardir, '', -2, '<DIR>', -1, '', -1,
					'', True, True, False, None, 'go-up', None, 0, 0, None, ''
					))

			else:
				# prepare full parent path
				parent_path = self._store.get_value(parent, Column.NAME)

		# load items in separate thread
		def thread_method():
			self._thread_active.set()
			Gdk.threads_add_idle(GLib.PRIORITY_DEFAULT_IDLE, self._title_bar.show_spinner)

			# preload emblems for faster operation
			self._emblem_cache = self._parent.emblem_manager.get_emblems_for_path(path)

			# get initial directory listing
			try:
				provider = self.get_provider()
				item_list = provider.list_dir(path)

			except Exception as error:
				print('Load directory error: ', str(error))

				# clear locks and exit
				self._thread_active.clear()
				self._main_thread_lock.clear()

				Gdk.threads_add_idle(GLib.PRIORITY_DEFAULT_IDLE, self._title_bar.hide_spinner)
				return

			# remove hidden files if we don't need them
			if not show_hidden:
				always_hidden = []

				# get list of always hidden files from the directory file
				if provider.exists('.hidden', relative_to=path):
					raw_file = provider.get_file_handle('.hidden', FileMode.READ, relative_to=path)
					always_hidden.extend(raw_file.read().splitlines())
					raw_file.close()

				# override hidden list with always visible items
				always_hidden = [item for item in always_hidden if item not in self._always_visible_items]

				# filter out hidden items and backup files
				item_list = [name for name in item_list if (name[0] != '.' and name[-1] != '~') or name in self._always_visible_items]

				# filter out items specified in directory file or program
				if len(always_hidden) > 0:
					item_list = [name for name in item_list if name not in always_hidden]

			# assign item for selection
			if not self._item_to_focus in item_list:
				self._item_to_focus = None

			for item_name in item_list:
				# check if we are allowed to continue as we don't want
				# items from different directory ending up in our list
				if not self._thread_active.is_set():
					break

				# add item to the list
				self._add_item(item_name, parent, parent_path)

			Gdk.threads_add_idle(GLib.PRIORITY_HIGH_IDLE, self._flush_queue, parent)

			# hide spinner animation
			Gdk.threads_add_idle(GLib.PRIORITY_DEFAULT_IDLE, self._title_bar.hide_spinner)

			# update status bar
			Gdk.threads_add_idle(GLib.PRIORITY_DEFAULT_IDLE, self._update_status_with_statistis)

			# release locks
			self._thread_active.clear()
			self._main_thread_lock.clear()

			# create directory monitor
			self.monitor_path(path, parent)

		# create new thread
		self._change_path_thread = Thread(target=thread_method)
		self._change_path_thread.start()

		# enable updates on cursor change
		self._item_list.handler_unblock_by_func(self._handle_cursor_change)

	def _update_emblems_by_name(self, name, parent=None, parent_path=None):
		"""Update emblem list for specified iter in list."""
		found_iter = self._find_iter_by_name(name, parent)

		if not found_iter:
			return

		# get path to load emblems for
		path = self._options.get('path')
		if parent is not None:
			path = os.path.join(path, parent_path)

		# get emblems for current path
		emblems = self._parent.emblem_manager.get_emblems(path, name)

		# update list
		self._store.set_value(found_iter, Column.EMBLEMS, emblems)

	def _update_directory_size_by_name(self, path, parent=None):
		"""Update directory size for specified iter in list."""
		found_iter = self._find_iter_by_name(path, parent)

		if not found_iter:
			return

		# format total size
		name = self._store.get_value(found_iter, Column.NAME)
		absolute_path = os.path.join(self.path, name)
		total_count, total_size = self._parent.disk_usage.get(self, absolute_path)
		formated_size = common.format_size(total_size, self._size_format, False)

		# update list
		self._store.set_value(found_iter, Column.FORMATED_SIZE, formated_size)

	def change_path(self, path=None, selected=None):
		"""Change file list path."""
		# cancel current directory monitor
		self.cancel_monitors()

		# cancel disk usage calculations
		self._parent.disk_usage.cancel_all_for_object(self)

		# hide thumbnail
		if self._enable_media_preview:
			self._thumbnail_view.hide()

		# get provider for specified URI
		provider = self.get_provider(path)

		# store path and scheme
		self.scheme = provider.get_protocol()
		if (self.scheme is None or self.scheme == 'file') and '://' in path:
			self.path = path.split('://')[1]
		else:
			self.path = path

		# update options container
		self._options.set('path', self.path)

		# update GTK controls
		path_name = os.path.basename(self.path)
		if path_name == "":
			path_name = self.path

		self._change_tab_text(common.decode_file_name(path_name))
		self._change_title_text(self.path)

		if self._parent.get_active_object() is self:
			self._parent.set_location_label(common.decode_file_name(self.path))

		# change list icon
		self._title_bar.set_icon_from_name(provider.get_protocol_icon())

		# reset directory statistics
		self._dirs['count'] = 0
		self._dirs['selected'] = 0
		self._files['count'] = 0
		self._files['selected'] = 0
		self._size['total'] = 0
		self._size['selected'] = 0

		try:
			# populate list
			if not provider.exists(self.path):
				raise OSError(_('No such file or directory'))

			self._item_to_focus = selected
			self._load_directory(self.path, clear_store=True)

			# if no errors occurred during path change,
			# call parent method which handles history
			ItemList.change_path(self, self.path)

		except OSError as error:
			# problem with listing directory, ask user what to do
			# TODO: Solve problem when program is not able to go up the
			# history lane but can't change to specified directory.
			dialog = Gtk.MessageDialog(
									self._parent,
									Gtk.DialogFlags.DESTROY_WITH_PARENT,
									Gtk.MessageType.ERROR,
									Gtk.ButtonsType.YES_NO,
									_(
										"Error changing working directory to:"
										"\n{1}\n\n{0}\n\nWould you like to retry?"
									).format(error, common.decode_file_name(path))
								)
			dialog.set_default_response(Gtk.ResponseType.YES)
			result = dialog.run()
			dialog.destroy()

			# remove invalid paths from history so we don't end up in a dead loop
			self.history = [history_path for history_path in self.history if path != history_path]

			# make sure we have something in history list
			if len(self.history) == 0:
				self.history.append(os.path.expanduser('~'))

			if result == Gtk.ResponseType.YES:
				# retry loading path again
				self.change_path(path)

			else:
				# load previous valid path
				self.change_path(self.history[0], os.path.basename(path))

			return

		# if no item was specified, select first one
		if selected in (None, os.path.pardir) \
		and len(self._store) > 0:
			path = self._store.get_path(self._store.get_iter_first())
			self._item_list.set_cursor(path)

	def select_all(self, pattern=None, exclude_list=None):
		"""Select all items matching pattern"""
		if pattern is None:
			pattern = "*"

		if exclude_list is None:
			exclude_list = ()

		dirs = 0
		files = 0
		size = 0
		result = 0

		for row in self._store:
			# set selection
			if not row[Column.IS_PARENT_DIR] \
			and fnmatch.fnmatch(row[Column.NAME], pattern) \
			and row[Column.NAME] not in exclude_list:
				# select item that matched out criteria
				row[Column.COLOR] = self._selection_color
				row[Column.SELECTED] = True

				result += 1

			elif len(exclude_list) > 0:
				# if out exclude list has items, we need to deselect them
				row[Column.COLOR] = None
				row[Column.SELECTED] = False

			# update dir/file count
			if row[Column.SELECTED]:
				if row[Column.IS_DIR]:
					dirs += 1
				else:
					files += 1
					size += row[Column.SIZE]

		self._dirs['selected'] = dirs
		self._files['selected'] = files
		self._size['selected'] = size

		# update status bar
		ItemList.select_all(self, pattern, exclude_list)
		self._update_status_with_statistis()

		return result

	def deselect_all(self, pattern=None):
		"""Deselect items matching the pattern"""
		if pattern is None:
			pattern = "*"

		dirs = 0
		files = 0
		size = 0
		result = 0

		for row in self._store:
			# set selection
			if not row[Column.IS_PARENT_DIR] and fnmatch.fnmatch(row[Column.NAME], pattern):
				row[Column.COLOR] = None
				row[Column.SELECTED] = False

				result += 1

			# update dir/file count
			if row[Column.SELECTED]:
				if row[Column.IS_DIR]:
					dirs += 1
				else:
					files += 1
					size += row[Column.SIZE]

		self._dirs['selected'] = dirs
		self._files['selected'] = files
		self._size['selected'] = size

		# update status bar
		ItemList.select_all(self, pattern)
		self._update_status_with_statistis()

		return result

	def invert_selection(self, pattern=None):
		"""Invert selection matching the pattern"""
		if pattern is None:
			pattern = "*"

		dirs = 0
		files = 0
		size = 0
		result = 0

		for row in self._store:
			# set selection
			if not row[Column.IS_PARENT_DIR] and fnmatch.fnmatch(row[Column.NAME], pattern):
				if not row[Column.SELECTED]:
					row[Column.COLOR] = self._selection_color
					row[Column.SELECTED] = True
				else:
					row[Column.COLOR] = None
					row[Column.SELECTED] = False

			# update dir/file count
			if row[Column.SELECTED]:
				if row[Column.IS_DIR]:
					dirs += 1
				else:
					files += 1
					size += row[Column.SIZE]

				result += 1

		self._dirs['selected'] = dirs
		self._files['selected'] = files
		self._size['selected'] = size

		# update status bar
		ItemList.select_all(self, pattern)
		self._update_status_with_statistis()

		return result

	def refresh_file_list(self, widget=None, data=None):
		"""Reload file list for current directory"""
		selection = self._item_list.get_selection()
		item_list, selected_iter = selection.get_selected()

		# get current selection
		f_name = None
		if selected_iter is not None:
			f_name = item_list.get_value(selected_iter, Column.NAME)

		# reload path
		self.change_path(self.path, f_name)

		return True

	def update_column_size(self, name):
		"""Update column size with global value"""
		column = [item for item in self._columns if item.name == name][0]
		width = self._parent.plugin_options.section(self._name).get('size_{0}'.format(name))

		if width is not None:
			column.set_fixed_width(width)

	def apply_settings(self):
		"""Apply file list settings"""
		ItemList.apply_settings(self)  # let parent apply its own settings
		section = self._parent.options.section('item_list')
		plugin_options = self._parent.plugin_options

		# load list of always visible items
		self._always_visible_items = section.get('always_visible')

		# apply column visibility and sizes
		self._reorder_columns()
		self._resize_columns(self._columns)
		self._set_font_size(self._columns)

		# apply row hinting
		row_hinting = section.get('row_hinting')
		self._item_list.set_rules_hint(row_hinting)

		# apply expander visibility
		self._show_expanders = section.get('show_expanders')
		self._item_list.set_show_expanders(self._show_expanders)

		# apply grid lines
		grid_lines = (
					Gtk.TreeViewGridLines.NONE,
					Gtk.TreeViewGridLines.HORIZONTAL,
					Gtk.TreeViewGridLines.VERTICAL,
					Gtk.TreeViewGridLines.BOTH,
				)[section.get('grid_lines')]
		self._item_list.set_grid_lines(grid_lines)

		# cache settings
		self._time_format = section.get('time_format')
		self._mode_format = section.get('mode_format')

		if plugin_options.has_section(self._name) \
		and plugin_options.section(self._name).has('columns'):
			self._show_full_name = 'extension' not in plugin_options.section(self._name).get('columns')

		else:
			self._show_full_name = False

		# reload file list in order to apply time formatting, hidden files and other
		self.refresh_file_list()

	def apply_media_preview_settings(self):
		"""Apply settings related to image_preview"""
		self._enable_media_preview = self._parent.options.get('media_preview')

		if self._enable_media_preview:
			# force showing thumbnail
			self._handle_cursor_change()

		else:
			# hide thumbnail
			self._thumbnail_view.hide()
