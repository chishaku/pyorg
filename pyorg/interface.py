import os
from pathlib import Path
import json
from abc import ABC, abstractmethod
from collections.abc import MutableMapping
from tempfile import TemporaryDirectory

from emacs.elisp import E
from .io import org_doc_from_json, agenda_item_from_json



def export_org_file_el(file, dest):
	"""Create elisp code to export org file as JSON.

	Parameters
	----------
	file : str
		Absolute path to org file to be exported.
	dest : str
		Absolute path to write exported data to.

	Returns
	-------
	emacs.elisp.Form
	"""
	return E.with_current_buffer(
		E.find_file_noselect(str(file)),
		E.org_export_to_file(E.Q('json'), str(dest))
	)


class OrgDirectory:
	"""The directory where the user's org files are kept.

	path : pathlib.Path
		Absolute path to org directory.
	"""

	def __new__(cls, path):
		# Return argument when called with existing instance.
		if isinstance(path, OrgDirectory):
			return path
		return object.__new__(cls)

	def __init__(self, path):
		if path is self:
			return
		self.path = Path(path).expanduser().absolute()

	def __repr__(self):
		return '%s(%r)' % (type(self).__name__, str(self.path))

	def get_abs_path(self, path, outside_ok=False):
		"""Get absolute path from path relative to org directory.

		Path will be normalized with any ".." components removed. Absolute paths
		are passed through.

		Parameters
		----------
		path : str or pathlib.Path
			A file path. If relative it is interpreted as being relative to the
			org directory.
		outside_ok : bool
			If False and the resulting path is outside of the org directory
			raise an exception.

		Returns
		-------
		pathlib.Path
			Absolute and normalized version of ``path``.

		Raises
		------
		ValueError
			If the path is outside of the org directory and ``outside_ok`` is
			False.
		"""
		path = Path(path)

		if not path.is_absolute():
			path = self.path / path

		path = Path(os.path.normpath(str(path)))

		if not outside_ok and not path.parts[:len(self.path.parts)] == self.path.parts:
			raise ValueError('Path must be contained in %s' % self.path)

		return path

	def get_rel_path(self, path, outside_ok=False):
		"""Convert path to one relative to org directory.

		Path will be normalized with any ".." components removed.

		Parameters
		----------
		path : str or pathlib.Path
			A file path. If relative it is interpreted as being relative to the
			org directory.
		outside_ok : bool
			If False and the resulting path is outside of the org directory
			raise an exception.

		Returns
		-------
		pathlib.Path
			Version of ``path`` relative to org directory.

		Raises
		------
		ValueError
			If the path is outside of the org directory and ``outside_ok`` is
			False.
		"""
		path = Path(os.path.normpath(str(path)))

		if path.is_absolute():
			path = path.relative_to(self.path)

		if not outside_ok and path.parts[0] == '..':
			raise ValueError('Path must be contained in %s' % self.path)

		return path

	def list_files(self, path=None, recursive=False, hidden=False):
		"""List org files within the org directory.

		Paths are relative to the org directory.

		Parameters
		----------
		path : str or pathlib.Path
			Optional subdirectory to search through.
		recursive : bool
			Recurse through subdirectories.
		hidden : bool
			Include hidden files.

		Returns
		-------
			Iterator over :class:`pathlib.Path` instances.
		"""
		abspath = self.path if path is None else self.get_abs_path(path)
		pattern = '**/*.org' if recursive else '*.org'

		for file in abspath.glob(pattern):
			if hidden or not file.name.startswith('.'):
				yield file.relative_to(self.path)

	def _get_org_file(self, path):
		"""Convert path to absolute, ensuring it is an org file within the directory.

		Parameters
		----------
		path : str or pathlib.Path

		Returns
		-------
		pathlib.Path

		Raises
		------
		ValueError
			If path is not within org directory or does not have .org extension.
		OSError
			If path is not a file.
		"""
		path = self.get_abs_path(path)

		if not path.is_file():
			raise OSError('%s is not a file' % path)
		if path.suffix != '.org':
			raise ValueError('Must be an org file')

		return path


class OrgFileLoader(ABC):
	"""Base for classes which can load org mode files, by Emacs export or other means.
	"""

	@abstractmethod
	def load_file(self, file):
		"""Load an org file.

		Parameters
		----------
		file : str
			File name/path, relative to org directory.

		Returns
		-------
		pyorg.ast.OrgDocument
		"""


class DirectFileLoader(OrgFileLoader):
	"""Loads org files directly from Emacs using ox-JSON exporter."""

	def __init__(self, emacs, orgdir=None):
		self.emacs = emacs
		self.orgdir = None if orgdir is None else OrgDirectory(orgdir)

	def load_file(self, file, raw=False):
		# Load by having Emacs export to temporary file and then parsing that,
		# because I was having issues reading data from stdout.

		file = Path(file)
		if not file.is_absolute():
			if self.orgdir:
				file = self.orgdir.get_abs_path(file, outside_ok=True)
			else:
				file = file.absolute()

		if not file.is_file():
			raise FileNotFoundError(file)

		with TemporaryDirectory() as tmpdir:
			tmpfile = os.path.join(tmpdir, file.stem + '.json')
			el = export_org_file_el(file, tmpfile)
			self.emacs.eval(el)
			with open(tmpfile, encoding='utf8') as f:
				data = json.load(f)

		return data if raw else org_doc_from_json(data)


class OrgFileCache(OrgFileLoader, MutableMapping):
	"""Base for classes which cache exported org files.
	"""

	def __setitem__(self, file, value):
		raise TypeError('Item assignment not supported')


class OrgFilesystemCache(OrgFileCache):
	"""Caches exported org file data in file system.

	Attributes
	----------
	base_dir : .OrgDirectory
		Base directory relative to which source .org files are located. May or
		may not be the user's actual org directory in Emacs.
	cache_dir : pathlib.Path
		Path to the directory holding the cached files.
	emacs : emacs.Emacs
	"""

	def __init__(self, base_dir, cache_dir, emacs):
		self.base_dir = OrgDirectory(base_dir)
		self.cache_dir = Path(cache_dir).absolute()
		self.emacs = emacs
		self.ext = '.org.json'

	def _locate_cached(self, file):
		"""Get the path to the cached file."""
		file = self.base_dir.get_rel_path(file)
		return self.cache_dir / file.parent / (file.stem + self.ext)

	def get_timestamp(self, file):
		"""Get the timestamp indicating when the cached file was exported."""
		return self._locate_cached(file).stat().st_mtime_ns

	def is_valid(self, file):
		"""Check if the cached file is valid."""
		cached = self._locate_cached(file)
		if not cached.is_file():
			raise KeyError(file)

		src = self.base_dir.get_abs_path(file)

		# Check it still exists
		if not src.is_file():
			return False

		# Check not out of date
		return src.stat().st_mtime < cached.stat().st_mtime

	def _store(self, file):
		"""Export an org file to store it in the cache."""
		src = self.base_dir.get_abs_path(file)
		dest = self._locate_cached(file)
		dest.parent.mkdir(parents=True, exist_ok=True)
		el = export_org_file_el(str(src), str(dest))
		self.emacs.eval(el)

	def _read(self, file):
		"""Read file from the cache."""
		with self._locate_cached(file).open(encoding='utf8') as f:
			return json.load(f)

	def _remove(self, file):
		"""Remove file from cache."""
		cached = self._locate_cached(file)
		cached.unlink()

		# Remove parent directory if empty
		try:
			cached.parent.rmdir()
		except OSError:
			pass

	def _validate(self, file):
		"""Check if cached file is valid, removing it if it isn't.

		Returns
		-------
		bool
		"""
		if not self.is_valid(file):
			self._remove(file)
			return False

		return True

	def _get_data(self, file):
		if file not in self or not self.is_valid(file):
			self._store(file)
		return self._read(file)

	def load_file(self, file, raw=False):
		data = self._get_data(file)
		return data if raw else org_doc_from_json(data)

	def __contains__(self, file):
		return self._locate_cached(file).is_file()

	def __iter__(self):
		for path in self.cache_dir.glob('**/*' + self.ext):
			yield str(path.relative_to(self.cache_dir))

	def __len__(self):
		return sum(1 for file in self)

	def __getitem__(self, file):
		try:
			return self._read(file)
		except FileNotFoundError:
			raise KeyError(file) from None

	def __delitem__(self, file):
		try:
			self._remove(file)
		except FileNotFoundError:
			raise KeyError(file) from None


class Org:
	"""Interface to org mode.

	Attributes
	----------
	emacs : pyorg.emacs.Emacs
	orgdir : .OrgDirectory
		Directory org files are read from.
	loader : .OrgFileLoader
		Loader used to read .org file data.
	"""

	def __init__(self, emacs, orgdir=None, loader=None):
		"""
		Parameters
		----------
		emacs : pyorg.emacs.Emacs
		orgdir : str or pathlib.Path or .OrgDirectory
			Absolute path to org directory. If None will use value of
			``org-directory`` variable in Emacs.
		loader : .OrgFileLoader
			Loader to use to read .org file data.
		"""
		self.emacs = emacs
		self._setup_emacs()

		if orgdir is None:
			orgdir = self.emacs.getresult('org-directory')

		self.orgdir = OrgDirectory(orgdir)

		self.direct_loader = DirectFileLoader(self.emacs, self.orgdir)
		self.loader = self.direct_loader if loader is None else loader

	def _setup_emacs(self):
		"""Perform initial setup with Emacs."""
		self.emacs.eval(E.require(E.Q('ox-json')))

	def read_org_file_direct(self, file, raw=False):
		"""Read and parse an org file directly from Emacs.

		Always reads the current file and does not use cached data, or perform
		any additional processing other than parsing.

		Parameters
		----------
		file : str or pathlib.Path
			Path to file to load (relative paths are interpreted relative to
			org directory).
		raw : bool
			Don't parse and just return raw JSON exported from Emacs.

		Returns
		-------
		pyorg.ast.OrgDocument or dict

		Raises
		------
		FileNotFoundError
		"""
		return self.direct_loader.load_file(file, raw=raw)

	def read_org_file(self, file, raw=None):
		"""Read and parse an org file.

		Parameters
		----------
		file : str or pathlib.Path
			Path to file to load (relative paths are interpreted relative to
			org directory).
		raw : bool
			Don't parse and just return raw JSON exported from Emacs.

		Returns
		-------
		pyorg.ast.OrgDocument

		Raises
		------
		FileNotFoundError
		"""
		return self.loader.load_file(file, raw=raw)

	def open_org_file(self, path, focus=False):
		"""Open an org file in the org directory for editing in Emacs.

		Parameters
		----------
		path : str or pathlib.Path
			File path relative to org directory.
		focus : bool
			Switch window/input focus to opened buffer.
		"""
		path = self.orgdir._get_org_file(path)
		el = E.find_file(str(path))
		if focus:
			el = [el, E.x_focus_frame(None)]
		self.emacs.eval(el)

	def agenda(self, key='t', raw=False):
		"""TODO Read agenda information.

		Parameters
		----------
		key : str
			TODO

		Returns
		-------
		list[dict]
		"""

		el = E.org_json_with_agenda_buffer(
			key,
			E.org_json_encode_agenda_buffer()
		)
		result = self.emacs.getresult(el, encode=False)
		data = json.loads(result)
		if raw:
			return data
		return list(map(agenda_item_from_json, data))
