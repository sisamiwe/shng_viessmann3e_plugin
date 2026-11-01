#!/usr/bin/env python3
# vim: set encoding=utf-8 tabstop=4 softtabstop=4 shiftwidth=4 expandtab
#########################################################################
#  Copyright 2023-     <AUTHOR>                                   <EMAIL>
#########################################################################
#  This file is part of SmartHomeNG.
#  https://www.smarthomeNG.de
#  https://knx-user-forum.de/forum/supportforen/smarthome-py
#
#  This file implements the web interface for the Sample plugin.
#
#  SmartHomeNG is free software: you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
#
#  SmartHomeNG is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with SmartHomeNG. If not, see <http://www.gnu.org/licenses/>.
#
#########################################################################

import datetime
import time
import os
import json

from lib.item import Items
from lib.model.smartplugin import SmartPluginWebIf


# ------------------------------------------
#    Webinterface of the plugin
# ------------------------------------------

import cherrypy
import csv
from jinja2 import Environment, FileSystemLoader


class WebInterface(SmartPluginWebIf):
    def __init__(self, webif_dir, plugin):
        """
        Initialization of instance of class WebInterface

        :param webif_dir: directory where the webinterface of the plugin resides
        :param plugin: instance of the plugin
        :type webif_dir: str
        :type plugin: object
        """
        self.logger = plugin.logger
        self.webif_dir = webif_dir
        self.plugin = plugin
        self.items = Items.get_instance()

        self.tplenv = self.init_template_environment()

    @cherrypy.expose
    def index(self, reload=None, scan=None):
        """Build index.html for cherrypy.

        Render the template and return the html file to be delivered to the browser.

        :param reload: If set, triggers a reload of the page.
        :param scan: If set, initiates a device scan.
        :return: contents of the template after being rendered.
        """
        if scan:
            self._trigger_scan()

        pagelength = self.plugin.get_parameter_value('webif_pagelength')
        tmpl = self.tplenv.get_template('index.html')

        open3e_items = self.plugin.get_item_list()
        open3e_item_count = len(open3e_items)
        device_count = len(self.plugin.devices) if hasattr(self.plugin, 'devices') else 0

        dids = {}
        for ecu, client_entry in self.plugin.clients.items():
            target_client = client_entry['client']
            
            # DIDs für diese ECU abrufen und direkt als Dictionary aufbauen
            dids[ecu] = {
                did: {
                    'name': did_entry.get('id', ''),
                    'desc': did_entry.get('args', {}).get('desc', '')
                }
                for did, did_entry in target_client.list_dids().items()
            }

        total_dids = sum(len(ecu_dids) for ecu_dids in dids.values())

        return tmpl.render(plugin_shortname=self.plugin.get_shortname(),
                           plugin_version=self.plugin.get_version(),
                           plugin_info=self.plugin.get_info(),
                           open3e_items=open3e_items,
                           open3e_item_count=open3e_item_count,
                           device_count=device_count,
                           dids = dids,
                           total_dids = total_dids,
                           p=self.plugin,
                           webif_pagelength=pagelength,
                           hex=hex
                           )

    def _trigger_scan(self):
        """Trigger the depict_system scan in the plugin."""
        if hasattr(self.plugin, 'depict_system'):
            try:
                self.logger.info("WebIF: Starting device scan via depict_system")
                self.plugin.depict_system()
            except Exception as e:
                self.logger.error(f"WebIF: Error during device scan: {e}")
        else:
            self.logger.warning("WebIF: depict_system method not available")

    @cherrypy.expose
    def get_data_html(self, dataSet=None):
        """Return data to update the webpage.

        For the standard update mechanism of the web interface, the dataSet to return the data for is None.

        :param dataSet: Dataset for which the data should be returned (standard: None).
        :return: dict with the data needed to update the web page.
        """
        if dataSet == 'items_info':
            data = {}
            for item in self.plugin.get_active_items():
                prop = item.property
                val = prop.value
                
                data[prop.path] = {
                    'value': json.dumps(val) if isinstance(val, (dict, list)) else val,
                    'last_update': prop.last_update.strftime('%d.%m.%Y %H:%M:%S') if prop.last_update else '-',
                    'last_change': prop.last_change.strftime('%d.%m.%Y %H:%M:%S') if prop.last_change else '-',
                }
            
            try:
                return json.dumps(data)
            except Exception as e:
                self.logger.error(f'get_data_html items_info exception: {e}')

        elif dataSet == 'devices_info':
            data = {}
            for addr, entry in self.plugin.clients.items():
                client = entry.get('client')
                if client:
                    data[hex(addr)] = {
                        'name': entry.get('name', ''),
                        'connected': client.connected,
                        'did_count': len(client.data) if hasattr(client, 'data') else 0
                    }
            try:
                return json.dumps(data)
            except Exception as e:
                self.logger.error(f'get_data_html devices_info exception: {e}')

        elif dataSet == 'scan_info':
            data = {
                'scanner_version': getattr(self.plugin, 'SCANNER_VERSION', 'N/A') if hasattr(self.plugin, '_get_scanner') else 'N/A',
                'devices': len(self.plugin.devices) if hasattr(self.plugin, 'devices') else 0,
                'config_path': str(self.plugin.config_data_path) if hasattr(self.plugin, 'config_data_path') else ''
            }
            try:
                return json.dumps(data)
            except Exception as e:
                self.logger.error(f'get_data_html scan_info exception: {e}')

        elif dataSet == 'log_info':
            data = {
                'log_entries': [],
                'count': 0
            }
            try:
                return json.dumps(data)
            except Exception as e:
                self.logger.error(f'get_data_html log_info exception: {e}')

        return {}
