#!/usr/bin/env python3

# Binary Analysis Next Generation (BANG!)
#
# Copyright 2021 - Armijn Hemel
# Licensed under the terms of the GNU Affero General Public License version 3
# SPDX-License-Identifier: AGPL-3.0-only

'''
This script processes data crawled from the F-Droid repositories
and puts the relevant data in a PostgreSQL database.
'''

import sys
import os
import argparse
import stat
import pathlib
import zipfile
import datetime
import tempfile
import shutil
import hashlib

# import XML processing that guards against several XML attacks
import defusedxml.minidom

# import some modules for dependencies, requires psycopg2 2.7+
import psycopg2
import psycopg2.extras

# import YAML module for the configuration
from yaml import load
from yaml import YAMLError
try:
    from yaml import CLoader as Loader
except ImportError:
    from yaml import Loader


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-c", "--config", action="store", dest="cfg",
                        help="path to F-Droid configuration file", metavar="FILE")
    args = parser.parse_args()

    # sanity checks for the configuration file
    if args.cfg is None:
        parser.error("No configuration file provided, exiting")

    # the configuration file should exist ...
    if not os.path.exists(args.cfg):
        parser.error("File %s does not exist, exiting." % args.cfg)

    # ... and should be a real file
    if not stat.S_ISREG(os.stat(args.cfg).st_mode):
        parser.error("%s is not a regular file, exiting." % args.cfg)

    # read the configuration file. This is in YAML format
    try:
        configfile = open(args.cfg, 'r')
        config = load(configfile, Loader=Loader)
    except (YAMLError, PermissionError):
        print("Cannot open configuration file, exiting", file=sys.stderr)
        sys.exit(1)

    # some sanity checks:
    if 'database' not in config or 'general' not in config:
        print("Invalid configuration file, exiting", file=sys.stderr)
        sys.exit(1)

    for i in ['postgresql_user', 'postgresql_password', 'postgresql_db']:
        if i not in config['database']:
            print("Configuration file malformed: missing database information %s" % i,
                  file=sys.stderr)
            sys.exit(1)
        postgresql_user = config['database']['postgresql_user']
        postgresql_password = config['database']['postgresql_password']
        postgresql_db = config['database']['postgresql_db']

    # default values
    postgresql_host = None
    postgresql_port = None

    if 'postgresql_host' in config['database']:
        postgresql_host = config['database']['postgresql_host']
    if 'postgresql_port' in config['database']:
        postgresql_port = config['database']['postgresql_port']

    # test the database connection
    try:
        cursor = psycopg2.connect(database=postgresql_db, user=postgresql_user,
                                  password=postgresql_password,
                                  port=postgresql_port, host=postgresql_host)
        cursor.close()
    except psycopg2.Error:
        print("Database server not running or malconfigured, exiting.",
              file=sys.stderr)
        sys.exit(1)

    verbose = False
    if 'verbose' in config['general']:
        if isinstance(config['general']['verbose'], bool):
            verbose = config['general']['verbose']

    if 'storedirectory' not in config['general']:
        print("F-Droid store directory not defined, exiting", file=sys.stderr)
        sys.exit(1)

    store_directory = pathlib.Path(config['general']['storedirectory'])

    # Check if the base unpack directory exists
    if not store_directory.exists():
        print("Store directory %s does not exist, exiting" % store_directory,
              file=sys.stderr)
        sys.exit(1)

    if not store_directory.is_dir():
        print("Store directory %s is not a directory, exiting" % store_directory,
              file=sys.stderr)
        sys.exit(1)

    # directory for unpacking. By default this will be /tmp or whatever
    # the system default is.
    temporary_directory = None

    if 'tempdir' in config['general']:
        temporary_directory = pathlib.Path(config['general']['tempdir'])
        if temporary_directory.exists():
            if temporary_directory.is_dir():
                # check if the temporary directory is writable
                try:
                    temp_name = tempfile.NamedTemporaryFile(dir=temporary_directory)
                    temp_name.close()
                except:
                    temporary_directory = None
            else:
                temporary_directory = None
        else:
            temporary_directory = None

    dir_filter = []

    # filter for irrelevant files in META-INF that should not be stored
    # in the database as entries just eat space such as the various
    # support libraries, F-Droid support files, etc.
    meta_files_filter = ['META-INF/androidx.*.version',
                    'META-INF/com.android.support_*',
                    'META-INF/com.google.android.material_material.version',
                    'META-INF/android.arch.*', 'META-INF/android.support.*',
                    'META-INF/buildserverid', 'META-INF/fdroidserverid',
                    'META-INF/kotlinx-*.kotlin_module',
                    'META-INF/kotlin-*.kotlin_module']

    # filter for irrelevant directories that should not be stored in the
    # database as entries just eat space such as the various support
    # libraries, time zone files, F-Droid support files, etc.
    dir_filter = ['zoneinfo/', 'zoneinfo-global/',
                  'org/joda/time/', 'kotlin/', 'kotlinx/']

    # get the latest XML file that was downloaded and process it
    # format is index.xml-%Y%m%d-%H%M%S
    xml_files = store_directory.glob('xml/index.xml-*')

    latest_xml = ''

    for i in xml_files:
        if latest_xml == '':
            latest_xml = i
            latest_timestamp = datetime.datetime.strptime(i.name, "index.xml-%Y%m%d-%H%M%S")
        else:
            timestamp = datetime.datetime.strptime(i.name, "index.xml-%Y%m%d-%H%M%S")
            if timestamp > latest_timestamp:
                latest_xml = i
                latest_timestamp = timestamp

    if latest_xml == '':
        print("No valid F-Droid XML file found', exiting", file=sys.stderr)
        sys.exit(1)

    # now open the XML file to see if it is valid XML data, else exit
    try:
        fdroidxml = defusedxml.minidom.parse(latest_xml.open())
    except:
        print("Could not parse F-Droid XML %s, exiting." % latest_xml, file=sys.stderr)
        sys.exit(1)

    # open a connection to the database
    dbconnection = psycopg2.connect(database=postgresql_db,
                                    user=postgresql_user,
                                    password=postgresql_password,
                                    port=postgresql_port,
                                    host=postgresql_host)
    dbcursor = dbconnection.cursor()

    # create a prepared statement
    preparedmfg = "PREPARE apk_insert as INSERT INTO apk_contents (apkname, fullfilename, filename, sha256) values ($1, $2, $3, $4) ON CONFLICT DO NOTHING"
    dbcursor.execute(preparedmfg)

    # Process the XML. Each application can have several
    # packages (versions) associated with it. The application
    # information is identical for every package.
    application_counter = 0
    apk_counter = 0
    total_files = 0
    for i in fdroidxml.getElementsByTagName('application'):
        application_id = ''
        application_license = ''
        source_url = ''
        for childnode in i.childNodes:
            if childnode.nodeName == 'id':
                application_id = childnode.childNodes[0].data
                application_counter += 1
            elif childnode.nodeName == 'source':
                if childnode.childNodes != []:
                    source_url = childnode.childNodes[0].data
            elif childnode.nodeName == 'license':
                application_license = childnode.childNodes[0].data
            elif childnode.nodeName == 'package':
                # store files and hashes
                apk_hashes = []
                apk_success = True
                for packagenode in childnode.childNodes:
                    if packagenode.nodeName == 'srcname':
                        srcname = packagenode.childNodes[0].data
                    elif packagenode.nodeName == 'hash':
                        apk_hash = packagenode.childNodes[0].data
                    elif packagenode.nodeName == 'version':
                        apk_version = packagenode.childNodes[0].data
                    elif packagenode.nodeName == 'apkname':
                        apkname = packagenode.childNodes[0].data
                        apkfile = store_directory / 'binary' / apkname
                        # verify if the APK actually has been downloaded
                        if not apkfile.exists():
                            apk_success = False
                            break
                        # verify if the APK is a valid zip file
                        if not zipfile.is_zipfile(apkfile):
                            apk_success = False
                            break

                        apk_zip = zipfile.ZipFile(apkfile)
                        # scan the contents of the APK
                        # 1. create a temporary directory
                        tempdir = tempfile.mkdtemp(dir=temporary_directory)

                        # 2. unpack the APK
                        try:
                            apk_zip.extractall(path=tempdir)
                        except zipfile.BadZipFile:
                            shutil.rmtree(tempdir)
                            apk_success = False
                            break

                        # 3. hash the contents of each file
                        old_dir = os.getcwd()
                        os.chdir(tempdir)

                        # unfortunately pathlib does not yet have a recursive iterdir()
                        for apk_entries in os.walk('.'):
                            for dir_entry in apk_entries[2]:
                                apk_entry_name = pathlib.PurePosixPath(apk_entries[0], dir_entry)
                                apk_entry = pathlib.Path(apk_entry_name)
                                if not apk_entry.is_file() or apk_entry.is_symlink():
                                    # skip non-files
                                    continue
                                if apk_entry.stat().st_size == 0:
                                    # skip empty files
                                    continue

                                # filter irrelevant directories
                                filter_matched = False
                                for file_filter in dir_filter:
                                    if apk_entry.is_relative_to(file_filter):
                                        filter_matched = True
                                        break
                                if filter_matched:
                                    continue
                                # filter irrelevant files
                                if apk_entry.is_relative_to('META-INF'):
                                    for file_filter in meta_files_filter:
                                        if apk_entry.match(file_filter):
                                            filter_matched = True
                                            break
                                    if filter_matched:
                                        continue
                                apk_entry_hash = hashlib.new('sha256')
                                apk_entry_hash.update(apk_entry.read_bytes())
                                apk_hashes.append((apkname, str(apk_entry), apk_entry.name,
                                                   apk_entry_hash.hexdigest()))
                        os.chdir(old_dir)

                        # 4. clean up
                        shutil.rmtree(tempdir)

                if apk_success:
                    # insert meta information about the APK
                    dbcursor.execute("INSERT INTO fdroid_package (identifier, version, apkname, sha256, srcpackage) VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                                     (application_id, apk_version, apkname, apk_hash, srcname))
                    dbconnection.commit()

                    # insert contents of all the files in the APK
                    psycopg2.extras.execute_batch(dbcursor, "execute apk_insert(%s, %s, %s, %s)", apk_hashes)
                    dbconnection.commit()

                    apk_counter += 1
                    total_files += len(apk_hashes)
                    if verbose:
                        print("Processing %d: %s" % (apk_counter, apkname))

        # insert meta information about the application
        dbcursor.execute("INSERT INTO fdroid_application (identifier, source, license) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                         (application_id, source_url, application_license))
        dbconnection.commit()

    if verbose:
        print()
        if application_counter == 1:
            print("Processed: 1 application")
        else:
            print("Processed: %d applications" % application_counter)
        print("Processed: %d APK files" % apk_counter)
        print("Processed: %d individual files" % total_files)

    # cleanup
    dbconnection.commit()

    # close the database connection
    dbcursor.close()
    dbconnection.close()

if __name__ == "__main__":
    main()
