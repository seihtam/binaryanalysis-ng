# Binary Analysis Next Generation (BANG!)
#
# This file is part of BANG.
#
# BANG is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License, version 3,
# as published by the Free Software Foundation.
#
# BANG is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public
# License, version 3, along with BANG.  If not, see
# <http://www.gnu.org/licenses/>
#
# Copyright Armijn Hemel
# Licensed under the terms of the GNU Affero General Public License
# version 3
# SPDX-License-Identifier: AGPL-3.0-only

'''
Extract bootloader files as found on Qualcomm Snapdragon (MSM)
based Android devices.
'''

import os
import pathlib
from FileResult import FileResult

from UnpackParser import UnpackParser, check_condition
from UnpackParserException import UnpackParserException
from kaitaistruct import ValidationNotEqualError
from . import android_msm_bootldr


class AndroidBootHuaweiUnpackParser(UnpackParser):
    extensions = []
    signatures = [
        (0, b'BOOTLDR!')
    ]
    pretty_name = 'androidmsmboot'

    def parse(self):
        file_size = self.fileresult.filename.stat().st_size
        try:
            self.data = android_msm_bootldr.AndroidMsmBootldr.from_io(self.infile)
        except (Exception, ValidationNotEqualError) as e:
            raise UnpackParserException(e.args)
        self.unpacked_size = self.data.ofs_start
        for entry in self.data.img_info:
            self.unpacked_size += entry.size
        check_condition(file_size >= self.unpacked_size, "not enough data")


    # no need to carve from the file
    def carve(self):
        pass

    def unpack(self):
        unpacked_files = []
        cur_offset = self.data.ofs_start
        for entry in self.data.img_info:
            if entry.size == 0:
                continue
            if entry.name == '':
                cur_offset += entry.size
                continue

            out_labels = []
            file_path = pathlib.Path(entry.name)
            self.extract_to_file(self.rel_unpack_dir / file_path, cur_offset, entry.size)
            fr = FileResult(self.fileresult, self.rel_unpack_dir / file_path, set(out_labels))
            unpacked_files.append(fr)
            cur_offset += entry.size
        return unpacked_files

    # make sure that self.unpacked_size is not overwritten
    def calculate_unpacked_size(self):
        pass

    def set_metadata_and_labels(self):
        """sets metadata and labels for the unpackresults"""
        labels = ['android', 'bootloader']
        metadata = {}

        metadata['chipset'] = 'snapdragon'
        metadata['partitions'] = []
        cur_offset = self.data.ofs_start
        for entry in self.data.img_info:
            if entry.size == 0:
                continue
            metadata['partitions'].append({'name': entry.name,
                                           'offset': cur_offset,
                                           'size': entry.size})
            cur_offset += entry.size

        self.unpack_results.set_labels(labels)
        self.unpack_results.set_metadata(metadata)
