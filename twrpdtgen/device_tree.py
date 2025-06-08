#
# Copyright (C) 2022 The Android Open Source Project
#
# SPDX-License-Identifier: Apache-2.0
#

from datetime import datetime
from git import Repo
from os import chmod
from pathlib import Path
from sebaubuntu_libs.libaik import AIKManager
from sebaubuntu_libs.libandroid.device_info import DeviceInfo
from sebaubuntu_libs.libandroid.fstab import Fstab
from sebaubuntu_libs.libandroid.props import BuildProp
from sebaubuntu_libs.liblogging import LOGD
from shutil import copyfile, rmtree
from stat import S_IRWXU, S_IRGRP, S_IROTH
from twrpdtgen import __version__ as version
from twrpdtgen.templates import render_template
from typing import List

# Ubicaciones extendidas para buscar build.prop
BUILDPROP_LOCATIONS = [
	Path() / "default.prop",
	Path() / "prop.default",
]

# Ubicaciones dentro del ramdisk extraído
BUILDPROP_LOCATIONS += [Path() / dir / "build.prop"
                        for dir in ["system", "vendor"]]
BUILDPROP_LOCATIONS += [Path() / dir / "etc" / "build.prop"
                        for dir in ["system", "vendor"]]

# Ubicaciones adicionales específicas para el contexto del dump
def get_extended_buildprop_locations(base_path: Path):
	"""Obtener todas las ubicaciones posibles donde puede estar build.prop"""
	locations = []
	
	# Ubicaciones estándar dentro del ramdisk
	for location in BUILDPROP_LOCATIONS:
		locations.append(base_path / location)
	
	# Obtener el directorio padre del ramdisk para buscar en el dump completo
	dump_path = base_path.parent
	
	# Verificar si estamos en una estructura de dump completo
	possible_dump_paths = [
		dump_path,  # directorio actual
		dump_path.parent,  # directorio padre
		dump_path.parent.parent,  # directorio abuelo
	]
	
	for dump_dir in possible_dump_paths:
		if not dump_dir.exists():
			continue
			
		# Ubicaciones específicas mencionadas por el usuario
		extended_locations = [
			# dump_path/system/system/build.prop
			dump_dir / "system" / "system" / "build.prop",
			dump_dir / "system" / "build.prop",
			
			# dump_path/vendor/build.prop
			dump_dir / "vendor" / "build.prop",
			
			# dump_path/vendor_boot/ramdisk/default.prop
			dump_dir / "vendor_boot" / "ramdisk" / "default.prop",
			dump_dir / "vendor_boot" / "ramdisk" / "prop.default",
			dump_dir / "vendor_boot" / "ramdisk" / "build.prop",
			dump_dir / "vendor_boot" / "ramdisk" / "system" / "build.prop",
			dump_dir / "vendor_boot" / "ramdisk" / "vendor" / "build.prop",
			
			# Otras ubicaciones comunes en dumps
			dump_dir / "system" / "etc" / "build.prop",
			dump_dir / "vendor" / "etc" / "build.prop",
			dump_dir / "product" / "build.prop",
			dump_dir / "system_ext" / "build.prop",
			dump_dir / "odm" / "build.prop",
			
			# Ubicaciones en boot/recovery extraídos
			dump_dir / "boot" / "ramdisk" / "default.prop",
			dump_dir / "boot" / "ramdisk" / "prop.default",
			dump_dir / "recovery" / "ramdisk" / "default.prop",
			dump_dir / "recovery" / "ramdisk" / "prop.default",
		]
		
		locations.extend(extended_locations)
	
	return locations

FSTAB_LOCATIONS = [Path() / "etc" / "recovery.fstab"]
FSTAB_LOCATIONS += [Path() / dir / "etc" / "recovery.fstab"
                    for dir in ["system", "vendor"]]

INIT_RC_LOCATIONS = [Path()]
INIT_RC_LOCATIONS += [Path() / dir / "etc" / "init"
                      for dir in ["system", "vendor"]]

class DeviceTree:
	"""
	A class representing a device tree

	It initialize a basic device tree structure
	and save the location of some important files
	"""
	def __init__(self, image: Path):
		"""Initialize the device tree class."""
		self.image = image

		self.current_year = str(datetime.now().year)

		# Check if the image exists
		if not self.image.is_file():
			raise FileNotFoundError("Specified file doesn't exist")

		# Extract the image
		self.aik_manager = AIKManager()
		self.image_info = self.aik_manager.unpackimg(image)

		# Determinar qué ramdisk usar basado en la información extraída
		ramdisk_path = None
		if self.image_info.ramdisk and self.image_info.ramdisk.is_dir():
			LOGD("Using standard ramdisk")
			ramdisk_path = self.image_info.ramdisk
		elif self.image_info.vendor_ramdisk and self.image_info.vendor_ramdisk.is_dir():
			LOGD("Using vendor_ramdisk (vendor_boot v4 detected)")
			ramdisk_path = self.image_info.vendor_ramdisk
		else:
			raise AssertionError("No valid ramdisk found (neither ramdisk nor vendor_ramdisk)")

		LOGD("Getting device infos...")
		self.build_prop = BuildProp()
		
		# Buscar archivos build.prop en ubicaciones extendidas
		build_prop_found = False
		search_locations = get_extended_buildprop_locations(ramdisk_path)
		
		LOGD(f"Searching for build.prop in {len(search_locations)} locations...")
		
		for build_prop_location in search_locations:
			if not build_prop_location.is_file():
				continue

			LOGD(f"Loading build.prop from {build_prop_location}")
			self.build_prop.import_props(build_prop_location)
			build_prop_found = True
			break  # Usar el primer build.prop encontrado

		# Si no se encuentra ningún build.prop, lanzar error
		if not build_prop_found:
			LOGD("Searched locations:")
			for location in search_locations:
				LOGD(f"  - {location} ({'EXISTS' if location.exists() else 'NOT FOUND'})")
			raise AssertionError("No build.prop file found in any of the searched locations")

		# Crear DeviceInfo con el build.prop encontrado
		self.device_info = DeviceInfo(self.build_prop)

		# Generate fstab
		fstab = None
		for fstab_location in [ramdisk_path / location for location in FSTAB_LOCATIONS]:
			if not fstab_location.is_file():
				continue

			LOGD(f"Generating fstab using {fstab_location} as reference...")
			fstab = Fstab(fstab_location)
			break

		if fstab is None:
			raise AssertionError("fstab not found")

		self.fstab = fstab

		# Search for init rc files
		self.init_rcs: List[Path] = []
		for init_rc_path in [ramdisk_path / location for location in INIT_RC_LOCATIONS]:
			if not init_rc_path.is_dir():
				continue

			self.init_rcs += [init_rc for init_rc in init_rc_path.iterdir()
			                  if init_rc.name.endswith(".rc") and init_rc.name != "init.rc"]

	def dump_to_folder(self, output_path: Path, git: bool = False) -> Path:
		device_tree_folder = output_path / self.device_info.manufacturer / self.device_info.codename
		prebuilt_path = device_tree_folder / "prebuilt"
		recovery_root_path = device_tree_folder / "recovery" / "root"

		LOGD("Creating device tree folders...")
		if device_tree_folder.is_dir():
			rmtree(device_tree_folder, ignore_errors=True)
		device_tree_folder.mkdir(parents=True)
		prebuilt_path.mkdir(parents=True)
		recovery_root_path.mkdir(parents=True)

		LOGD("Writing makefiles/blueprints")
		self._render_template(device_tree_folder, "Android.bp", comment_prefix="//")
		self._render_template(device_tree_folder, "Android.mk")
		self._render_template(device_tree_folder, "AndroidProducts.mk")
		self._render_template(device_tree_folder, "BoardConfig.mk")
		self._render_template(device_tree_folder, "device.mk")
		self._render_template(device_tree_folder, "extract-files.sh")
		self._render_template(device_tree_folder, "omni_device.mk", out_file=f"omni_{self.device_info.codename}.mk")
		self._render_template(device_tree_folder, "README.md")
		self._render_template(device_tree_folder, "setup-makefiles.sh")
		self._render_template(device_tree_folder, "vendorsetup.sh")

		# Set permissions
		chmod(device_tree_folder / "extract-files.sh", S_IRWXU | S_IRGRP | S_IROTH)
		chmod(device_tree_folder / "setup-makefiles.sh", S_IRWXU | S_IRGRP | S_IROTH)

		LOGD("Copying kernel...")
		if self.image_info.kernel is not None:
			copyfile(self.image_info.kernel, prebuilt_path / "kernel")
		if self.image_info.dt is not None:
			copyfile(self.image_info.dt, prebuilt_path / "dt.img")
		if self.image_info.dtb is not None:
			copyfile(self.image_info.dtb, prebuilt_path / "dtb.img")
		if self.image_info.dtbo is not None:
			copyfile(self.image_info.dtbo, prebuilt_path / "dtbo.img")

		LOGD("Copying fstab...")
		(device_tree_folder / "recovery.fstab").write_text(self.fstab.format(twrp=True))

		LOGD("Copying init scripts...")
		for init_rc in self.init_rcs:
			copyfile(init_rc, recovery_root_path / init_rc.name, follow_symlinks=True)

		if not git:
			return device_tree_folder

		# Create a git repo
		LOGD("Creating git repo...")

		git_repo = Repo.init(device_tree_folder)
		git_config_reader = git_repo.config_reader()
		git_config_writer = git_repo.config_writer()

		try:
			git_global_email, git_global_name = git_config_reader.get_value('user', 'email'), git_config_reader.get_value('user', 'name')
		except Exception:
			git_global_email, git_global_name = None, None

		if git_global_email is None or git_global_name is None:
			git_config_writer.set_value('user', 'email', 'barezzisebastiano@gmail.com')
			git_config_writer.set_value('user', 'name', 'Sebastiano Barezzi')

		git_repo.index.add(["*"])
		commit_message = self._render_template(None, "commit_message", to_file=False)
		git_repo.index.commit(commit_message)

		return device_tree_folder

	def _render_template(self, *args, comment_prefix: str = "#", **kwargs):
		return render_template(*args,
		                       comment_prefix=comment_prefix,
		                       current_year=self.current_year,
		                       device_info=self.device_info,
		                       fstab=self.fstab,
		                       image_info=self.image_info,
		                       version=version,
		                       **kwargs)

	def cleanup(self):
		# Cleanup
		self.aik_manager.cleanup()
