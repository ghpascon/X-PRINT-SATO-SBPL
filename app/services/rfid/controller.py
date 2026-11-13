"""
Docstring for app.services.rfid.controller
This module will be used for custom logic.
"""

from smartx_rfid.devices import DeviceManager
from smartx_rfid.utils import TagList
from smartx_rfid.dispatcher import EventDispatcher
from app.core import DISPATCHER_PATH, EXAMPLES_DISPATCHER_PATH
from .integration import Integration
import asyncio
from app.core import settings
import logging
from smartx_rfid.schemas.tag import WriteTagValidator
from smartx_rfid.db import DatabaseManager
from datetime import datetime


def _system_device(name: str) -> dict:
	"""Device descriptor for events that do not originate from a physical device."""
	return {'name': name, 'serial_number': None}


class Controller:
	def __init__(self, devices: DeviceManager, tags: TagList, integration: Integration):
		self.tags = tags
		self.devices = devices
		self.integration = integration
		self.dispatcher = EventDispatcher(
			dispatches_path=DISPATCHER_PATH,
			example_path=EXAMPLES_DISPATCHER_PATH,
		)
		self.write_list: dict = {}
		self.smtx_db = DatabaseManager(settings.SMTX_DB_URL)
		self.smtx_db.initialize()

		self.order_data = {}

	# [ EVENTS ]
	def on_event(self, device: dict, event_type: str, event_data):
		"""
		Handle a generic event.

		Args:
		    device: Device descriptor ``{'name': ..., 'serial_number': ...}``.
		    event_type: Event type.
		    event_data: Event payload.
		"""
		logging.info(
			f"[ EVENT ] {device.get('name')} (SN: {device.get('serial_number')}) - "
			f'{event_type}: {event_data}'
		)
		if not isinstance(self.order_data, dict):
			return

		if event_type in {'print_sent', 'print_sent_error', 'print_error', 'print_success'}:
			print_id = event_data.get('id') if isinstance(event_data, dict) else None
			print_record = self.order_data.get('print_ids', {}).get(print_id)
			if not print_record:
				return

		if event_type == 'print_sent':
			print_record['status'] = 'sent'
			self.save_print_to_db(event_data)
		elif event_type == 'print_sent_error':
			print_record['status'] = 'print_error'
		elif event_type == 'print_error':
			print_record['status'] = 'print_error'
		elif event_type == 'print_success':
			print_record['status'] = 'success'

	# [ Reading Events ]
	def on_start(self, device: dict):
		logging.info(f"[ START ] {device.get('name')}")
		# if not license_manager.validate_license():
		# 	return
		if settings.CLEAR_ON_START:
			self.tags.remove_tags_by_device(device=device)

	def on_stop(self, device: dict):
		logging.info(f"[ STOP ] {device.get('name')}")

	# [ Tag Events ]
	def on_new_tag(self, device: dict, tag: dict):
		logging.info(f"[ TAG ] {device.get('name')} - {tag}")

	def on_existing_tag(self, device: dict, tag: dict):
		asyncio.create_task(self.check_target(tag))

	# [ WRITE LIST ]
	def create_write_list_prefix(self, epcs: list, prefix: str):
		self.clear_write_list()
		for epc in epcs:
			target = f'{prefix}{epc[len(prefix):]}'
			current_tag = self.tags.get_by_identifier(epc)
			if current_tag:
				self.add_to_write_list(current_tag, target)
			else:
				logging.error(f'Epc: {epc} not in tags, skipping...')
		return self.write_list

	def add_to_write_list(self, tag: dict, target: str):
		self.write_list[tag.get('tid')] = {
			'target': target.lower(),
			'original_epc': tag.get('epc'),
		}
		tag['target'] = target
		logging.info(f"Added tag {tag.get('tid')} to write list with target {tag.get('target')}")
		self.on_event(
			device=_system_device('write_list'),
			event_type='add_to_write_list',
			event_data=tag,
		)

	def remove_from_write_list(self, tag: dict):
		tid = tag.get('tid')
		if tid in self.write_list:
			logging.info(f'Removed tag {tid} from write list')
			self.on_event(
				device=_system_device('write_list'),
				event_type='remove_from_write_list',
				event_data={**tag, 'original_epc': self.write_list[tid]['original_epc']},
			)
			del self.write_list[tid]
			if not self.write_list:
				logging.info('Write list is now empty')
				self.on_event(
					device=_system_device('write_list'),
					event_type='write_list_empty',
					event_data={},
				)
				return True
		return False

	def clear_write_list(self):
		self.write_list.clear()
		logging.info('Cleared write list')
		self.on_event(
			device=_system_device('write_list'),
			event_type='write_list_cleared',
			event_data={},
		)

	async def check_target(self, tag: dict):
		target = tag.get('target')
		if not target:
			return
		if tag.get('epc') == tag.get('target'):
			logging.info(f"Tag {tag.get('tid')} already has target EPC, removing from write list")
			tag['target'] = None
			self.remove_from_write_list(tag)
			return
		await self.devices.write_epc(
			device_name=tag.get('device'),
			write_tag=WriteTagValidator(
				target_identifier='tid',
				target_value=tag.get('tid'),
				new_epc=tag.get('target'),
				password='00000000',
			),
		)

	# SMTX QUERY
	def get_customers(self, params: dict | None = None):
		query = """
			SELECT id, name FROM customer
		"""
		return self.smtx_db.execute_query(query, params)

	def get_production_orders(self, customer_id: int):
		query = """
			SELECT id, idcode, quantity, serial_ini, serial_end FROM production_order WHERE customer_id = :customer_id
		"""
		return self.smtx_db.execute_query(query, {'customer_id': customer_id})

	def get_production_order_batches(self, production_order_id: int):
		query = """
			SELECT id, idcode FROM production_order_batch WHERE production_order_id = :production_order_id
		"""
		return self.smtx_db.execute_query(query, {'production_order_id': production_order_id})

	def set_order_data(
		self,
		customer_data: dict,
		production_order_data: dict,
		production_order_batch_data: dict,
	):
		self.order_data = {
			'customer_data': customer_data,
			'production_order_data': production_order_data,
			'production_order_batch_data': production_order_batch_data,
			'print_ids': {},
		}

	def get_already_printed(self, production_order_id: int, production_order_batch_id: int):
		query = """
			SELECT serial FROM production_order_tag
			WHERE production_order_id = :production_order_id
				AND production_order_batch_id = :production_order_batch_id
		"""
		return [
			row['serial']
			for row in self.smtx_db.execute_query(
				query,
				{
					'production_order_id': production_order_id,
					'production_order_batch_id': production_order_batch_id,
				},
			)
		]

	def start_production(self, device_name: str):
		if not self.order_data:
			logging.error('Order data is not set')
			return
		logging.info('Starting production with order data: %s', self.order_data)
		self.on_event(
			device=_system_device('production'),
			event_type='start_production',
			event_data={**self.order_data, 'device_name': device_name},
		)
		production_order = self.order_data['production_order_data']
		production_order_id = production_order['id']
		production_order_batch_id = self.order_data['production_order_batch_data']['id']
		already_printed = {
			str(serial)
			for serial in self.get_already_printed(
				production_order_id,
				production_order_batch_id,
			)
		}
		serials_to_print = [
			production_order['serial_ini'] + offset
			for offset in range(production_order['quantity'])
			if str(production_order['serial_ini'] + offset) not in already_printed
		]
		if not serials_to_print:
			logging.info('All serials for this production order batch are already printed')
			return

		sbpl = str(settings.SBPL)
		to_print = [sbpl.format(serial=serial) for serial in serials_to_print]
		ids = self.devices.add_to_print_queue(device_name, to_print)
		if not ids:
			raise RuntimeError('Failed to add to print queue')
		if isinstance(ids, str):
			ids = [ids]

		for print_id, serial in zip(ids, serials_to_print):
			self.order_data['print_ids'][print_id] = {
				'serial': serial,
				'status': 'pending',
			}

	def save_print_to_db(self, print_data: dict):
		if not self.order_data:
			raise RuntimeError('Order data is not set')
		event_print_data = print_data
		print_id = event_print_data.get('id')
		print_record = self.order_data['print_ids'].get(print_id)
		if not print_record:
			raise RuntimeError(f'Print ID {print_id} not found in order data')
		# Save print_data to the database here
		params = {
			'production_order_id': self.order_data.get('production_order_data', {}).get('id'),
			'production_order_batch_id': self.order_data.get('production_order_batch_data', {}).get(
				'id'
			),
			'serial': print_record.get('serial'),
			'barcode': str(print_record.get('serial')),
			'epc': str(print_record.get('serial')),
			'locked': 0,
			'reg_date': datetime.now(),
			'epc_original': event_print_data.get('epc_original', event_print_data.get('epc')),
			'tid': event_print_data.get('tid'),
		}
		update_query = """
			UPDATE production_order_tag
			SET barcode = :barcode,
				epc = :epc,
				locked = :locked,
				reg_date = :reg_date,
				epc_original = :epc_original,
				tid = :tid
			WHERE production_order_id = :production_order_id
				AND production_order_batch_id = :production_order_batch_id
				AND serial = :serial
		"""
		check_query = """
			SELECT 1 AS record_exists
			FROM production_order_tag
			WHERE production_order_id = :production_order_id
				AND production_order_batch_id = :production_order_batch_id
				AND serial = :serial
			LIMIT 1
		"""
		insert_query = """
			INSERT INTO production_order_tag (production_order_id, production_order_batch_id, serial, barcode, epc, locked, reg_date, epc_original, tid)
			VALUES (:production_order_id, :production_order_batch_id, :serial, :barcode, :epc, :locked, :reg_date, :epc_original, :tid)
		"""
		with self.smtx_db.get_session() as session:
			self.smtx_db.execute_query(update_query, params, session=session)
			record_exists = self.smtx_db.execute_query(
				check_query,
				{
					'production_order_id': params['production_order_id'],
					'production_order_batch_id': params['production_order_batch_id'],
					'serial': params['serial'],
				},
				session=session,
			)
			if not record_exists:
				self.smtx_db.execute_query(insert_query, params, session=session)

		logging.info('Saved print data to the database: %s', print_record)
