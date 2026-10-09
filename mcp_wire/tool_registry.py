# Enum registry: public enum sets + private inline enums
ENUM_REGISTRY = {
    "target_type": {"values": ['standby', 'private', 'group', 'all', 'all_in_station', 'all_in_area', 'all_in_system', 'all_in_region', 'pstn', 'pabx', 'sna', 'fssn', 'full_run', 'short_run'], "index": {'standby': 0, 'private': 1, 'group': 2, 'all': 3, 'all_in_station': 4, 'all_in_area': 5, 'all_in_system': 6, 'all_in_region': 7, 'pstn': 8, 'pabx': 9, 'sna': 10, 'fssn': 11, 'full_run': 12, 'short_run': 13}},
    "call_priority": {"values": ['normal', 'preemptive', 'emergency'], "index": {'normal': 0, 'preemptive': 1, 'emergency': 2}},
    "talk_mode": {"values": ['simplex', 'half_duplex', 'full_duplex'], "index": {'simplex': 0, 'half_duplex': 1, 'full_duplex': 2}},
    "work_mode": {"values": ['active', 'conventional', 'trunking', 'tmo', 'dmo', 'repeater'], "index": {'active': 0, 'conventional': 1, 'trunking': 2, 'tmo': 3, 'dmo': 4, 'repeater': 5}},
    "volume_type": {"values": ['call', 'tone', 'alarm', 'media'], "index": {'call': 0, 'tone': 1, 'alarm': 2, 'media': 3}},
    "tx_power_level": {"values": ['high', 'medium', 'low', 'auto', 'watt_0_3', 'watt_0_56', 'watt_1', 'watt_1_8', 'watt_3', 'watt_5_6', 'watt_10', 'watt_17_5', 'watt_30'], "index": {'high': 0, 'medium': 1, 'low': 2, 'auto': 3, 'watt_0_3': 4, 'watt_0_56': 5, 'watt_1': 6, 'watt_1_8': 7, 'watt_3': 8, 'watt_5_6': 9, 'watt_10': 10, 'watt_17_5': 11, 'watt_30': 12}},
    "hmi_profile": {"values": ['standard', 'silent', 'vibration', 'meeting', 'outdoor', 'flight'], "index": {'standard': 0, 'silent': 1, 'vibration': 2, 'meeting': 3, 'outdoor': 4, 'flight': 5}},
    "gps_mode": {"values": ['on', 'off', 'auto'], "index": {'on': 0, 'off': 1, 'auto': 2}},
    "channel_type": {"values": ['digital', 'analog'], "index": {'digital': 0, 'analog': 1}},
    "resource_name": {"values": ['all', 'system', 'radio', 'power', 'audio', 'zone', 'channel', 'contact', 'call', 'sms', 'network', 'scan', 'security', 'hmi', 'location', 'media', 'accessory', 'utility', 'folder'], "index": {'all': 0, 'system': 1, 'radio': 2, 'power': 3, 'audio': 4, 'zone': 5, 'channel': 6, 'contact': 7, 'call': 8, 'sms': 9, 'network': 10, 'scan': 11, 'security': 12, 'hmi': 13, 'location': 14, 'media': 15, 'accessory': 16, 'utility': 17, 'folder': 18}},
    "event_name": {"values": ['all', 'call_received', 'call_status_changed', 'sms_received'], "index": {'all': 0, 'call_received': 1, 'call_status_changed': 2, 'sms_received': 3}},
    "call_status": {"values": ['ringing', 'rejected', 'connected', 'talker', 'end'], "index": {'ringing': 0, 'rejected': 1, 'connected': 2, 'talker': 3, 'end': 4}},
    "audio_volume_set.mode": {"values": ['step', 'percent'], "index": {'step': 0, 'percent': 1}},
    "radio_find.mode": {"values": ['tone', 'vibrate', 'led', 'all'], "index": {'tone': 0, 'vibrate': 1, 'led': 2, 'all': 3}},
    "sms_play.filter": {"values": ['unread', 'all'], "index": {'unread': 0, 'all': 1}},
}

"""Auto-generated tool registry. DO NOT EDIT."""
"""Schema-driven int-key CBOR serialization."""

# Tool ID <-> name mappings (serialization-independent)

TOOL_NAME_TO_ID = {
    "accessory_bt_list": 1,
    "accessory_bt_pair": 2,
    "accessory_bt_switch": 3,
    "audio_microphone_switch": 4,
    "audio_volume_get": 5,
    "audio_volume_set": 6,
    "call_answer": 7,
    "call_end": 8,
    "call_start": 9,
    "contact_add": 10,
    "contact_delete": 11,
    "contact_update": 12,
    "contact_list": 13,
    "folder_group_active_get": 14,
    "folder_group_active_set": 15,
    "folder_group_active_index_set": 16,
    "hmi_dialog_confirm": 17,
    "hmi_profile_get": 18,
    "hmi_profile_set": 19,
    "hmi_vibration_switch": 20,
    "location_gps_switch": 21,
    "location_gps_get": 22,
    "location_report_trigger": 23,
    "media_record_play": 24,
    "power_battery_get": 25,
    "power_transmit_level_set": 26,
    "radio_alias_get": 27,
    "radio_alias_set": 28,
    "radio_id_get": 29,
    "radio_id_set": 30,
    "radio_time_get": 31,
    "radio_version_get": 32,
    "radio_find": 33,
    "radio_interconnect_switch": 34,
    "security_alert_start": 35,
    "security_alert_end": 36,
    "sms_send": 37,
    "sms_location_send": 38,
    "sms_play": 39,
    "sms_speak": 40,
    "system_event_subscribe": 41,
    "system_event_unsubscribe": 42,
    "utility_todo_add": 43,
    "zone_list": 44,
    "zone_delete": 45,
    "zone_add": 46,
    "zone_channel_list": 47,
    "zone_channel_active_get": 48,
    "zone_channel_active_set": 49,
    "zone_channel_active_index_set": 50,
}

TOOL_ID_TO_NAME = {
    1: "accessory_bt_list",
    2: "accessory_bt_pair",
    3: "accessory_bt_switch",
    4: "audio_microphone_switch",
    5: "audio_volume_get",
    6: "audio_volume_set",
    7: "call_answer",
    8: "call_end",
    9: "call_start",
    10: "contact_add",
    11: "contact_delete",
    12: "contact_update",
    13: "contact_list",
    14: "folder_group_active_get",
    15: "folder_group_active_set",
    16: "folder_group_active_index_set",
    17: "hmi_dialog_confirm",
    18: "hmi_profile_get",
    19: "hmi_profile_set",
    20: "hmi_vibration_switch",
    21: "location_gps_switch",
    22: "location_gps_get",
    23: "location_report_trigger",
    24: "media_record_play",
    25: "power_battery_get",
    26: "power_transmit_level_set",
    27: "radio_alias_get",
    28: "radio_alias_set",
    29: "radio_id_get",
    30: "radio_id_set",
    31: "radio_time_get",
    32: "radio_version_get",
    33: "radio_find",
    34: "radio_interconnect_switch",
    35: "security_alert_start",
    36: "security_alert_end",
    37: "sms_send",
    38: "sms_location_send",
    39: "sms_play",
    40: "sms_speak",
    41: "system_event_subscribe",
    42: "system_event_unsubscribe",
    43: "utility_todo_add",
    44: "zone_list",
    45: "zone_delete",
    46: "zone_add",
    47: "zone_channel_list",
    48: "zone_channel_active_get",
    49: "zone_channel_active_set",
    50: "zone_channel_active_index_set",
}

TOOL_NAME_TO_REQUEST_SCHEMA = {
    "accessory_bt_list": [
        {"name": "offset", "key": 1, "type": "integer", "default": 0},
        {"name": "limit", "key": 2, "type": "integer", "default": 10},
    ],
    "accessory_bt_pair": [
        {"name": "address", "key": 1, "type": "string", "default": None},
        {"name": "timeout", "key": 2, "type": "integer", "default": 30},
    ],
    "accessory_bt_switch": [
        {"name": "enabled", "key": 1, "type": "boolean", "default": None},
    ],
    "audio_microphone_switch": [
        {"name": "enabled", "key": 1, "type": "boolean", "default": None},
    ],
    "audio_volume_get": [
        {"name": "type", "key": 1, "type": "enum", "enum_ref": "volume_type", "default": "call"},
    ],
    "audio_volume_set": [
        {"name": "mode", "key": 1, "type": "enum", "enum_ref": "audio_volume_set.mode", "default": None},
        {"name": "value", "key": 2, "type": "integer", "default": None},
        {"name": "type", "key": 3, "type": "enum", "enum_ref": "volume_type", "default": "call"},
    ],
    "call_answer": [
    ],
    "call_end": [
    ],
    "call_start": [
        {"name": "target_type", "key": 1, "type": "enum", "enum_ref": "target_type", "default": None},
        {"name": "target_id", "key": 2, "type": "string", "default": ""},
        {"name": "priority", "key": 3, "type": "enum", "enum_ref": "call_priority", "default": "normal"},
        {"name": "talk_mode", "key": 4, "type": "enum", "enum_ref": "talk_mode", "default": "half_duplex"},
    ],
    "contact_add": [
        {"name": "name", "key": 1, "type": "string", "default": None},
        {"name": "contact_id", "key": 2, "type": "string", "default": None},
        {"name": "type", "key": 3, "type": "enum", "enum_ref": "target_type", "default": "private"},
    ],
    "contact_delete": [
        {"name": "items", "key": 1, "type": "array", "item_fields": [{"name": "contact_id", "key": 1, "type": "string"}, {"name": "type", "key": 2, "type": "enum", "enum_ref": "target_type"}]},
    ],
    "contact_update": [
        {"name": "contact_id", "key": 1, "type": "string", "default": None},
        {"name": "name", "key": 2, "type": "string", "default": None},
        {"name": "type", "key": 3, "type": "enum", "enum_ref": "target_type", "default": None},
        {"name": "is_favorite", "key": 4, "type": "boolean", "default": None},
    ],
    "contact_list": [
        {"name": "offset", "key": 1, "type": "integer", "default": 0},
        {"name": "limit", "key": 2, "type": "integer", "default": 10},
    ],
    "folder_group_active_get": [
        {"name": "mode", "key": 1, "type": "enum", "enum_ref": "work_mode", "default": "active"},
    ],
    "folder_group_active_set": [
        {"name": "folder_id", "key": 1, "type": "integer", "default": None},
        {"name": "group_id", "key": 2, "type": "integer", "default": None},
        {"name": "mode", "key": 3, "type": "enum", "enum_ref": "work_mode", "default": "active"},
    ],
    "folder_group_active_index_set": [
        {"name": "folder_index", "key": 1, "type": "integer", "default": None},
        {"name": "group_index", "key": 2, "type": "integer", "default": None},
        {"name": "mode", "key": 3, "type": "enum", "enum_ref": "work_mode", "default": "active"},
    ],
    "hmi_dialog_confirm": [
        {"name": "confirmed", "key": 1, "type": "boolean", "default": None},
    ],
    "hmi_profile_get": [
    ],
    "hmi_profile_set": [
        {"name": "profile", "key": 1, "type": "enum", "enum_ref": "hmi_profile", "default": None},
    ],
    "hmi_vibration_switch": [
        {"name": "enabled", "key": 1, "type": "boolean", "default": None},
    ],
    "location_gps_switch": [
        {"name": "mode", "key": 1, "type": "enum", "enum_ref": "gps_mode", "default": None},
    ],
    "location_gps_get": [
    ],
    "location_report_trigger": [
    ],
    "media_record_play": [
        {"name": "path", "key": 1, "type": "string", "default": ""},
        {"name": "start", "key": 2, "type": "integer", "default": -1},
        {"name": "number", "key": 3, "type": "integer", "default": 1},
    ],
    "power_battery_get": [
    ],
    "power_transmit_level_set": [
        {"name": "level", "key": 1, "type": "enum", "enum_ref": "tx_power_level", "default": None},
    ],
    "radio_alias_get": [
    ],
    "radio_alias_set": [
        {"name": "alias", "key": 1, "type": "string", "default": None},
    ],
    "radio_id_get": [
        {"name": "mode", "key": 1, "type": "enum", "enum_ref": "work_mode", "default": "active"},
    ],
    "radio_id_set": [
        {"name": "radio_id", "key": 1, "type": "string", "default": None},
        {"name": "mode", "key": 2, "type": "enum", "enum_ref": "work_mode", "default": "active"},
    ],
    "radio_time_get": [
    ],
    "radio_version_get": [
    ],
    "radio_find": [
        {"name": "duration", "key": 1, "type": "integer", "default": 10},
        {"name": "mode", "key": 2, "type": "enum", "enum_ref": "radio_find.mode", "default": "all"},
    ],
    "radio_interconnect_switch": [
        {"name": "enabled", "key": 1, "type": "boolean", "default": None},
    ],
    "security_alert_start": [
    ],
    "security_alert_end": [
    ],
    "sms_send": [
        {"name": "target_type", "key": 1, "type": "enum", "enum_ref": "target_type", "default": None},
        {"name": "target_id", "key": 2, "type": "string", "default": ""},
        {"name": "content", "key": 3, "type": "string", "default": None},
    ],
    "sms_location_send": [
        {"name": "target_type", "key": 1, "type": "enum", "enum_ref": "target_type", "default": None},
        {"name": "target_id", "key": 2, "type": "string", "default": ""},
        {"name": "value", "key": 3, "type": "string", "default": None},
        {"name": "latitude", "key": 4, "type": "float", "default": 0.0},
        {"name": "longitude", "key": 5, "type": "float", "default": 0.0},
    ],
    "sms_play": [
        {"name": "filter", "key": 1, "type": "enum", "enum_ref": "sms_play.filter", "default": "unread"},
        {"name": "start", "key": 2, "type": "integer", "default": -1},
        {"name": "number", "key": 3, "type": "integer", "default": 1},
    ],
    "sms_speak": [
        {"name": "text", "key": 1, "type": "string", "default": None},
        {"name": "save", "key": 2, "type": "boolean", "default": False},
    ],
    "system_event_subscribe": [
        {"name": "items", "key": 1, "type": "array", "item_fields": [{"name": "resource", "key": 1, "type": "enum", "enum_ref": "resource_name"}, {"name": "event", "key": 2, "type": "enum", "enum_ref": "event_name", "default": "all"}]},
    ],
    "system_event_unsubscribe": [
        {"name": "items", "key": 1, "type": "array", "item_fields": [{"name": "resource", "key": 1, "type": "enum", "enum_ref": "resource_name", "default": "all"}, {"name": "event", "key": 2, "type": "enum", "enum_ref": "event_name", "default": "all"}]},
    ],
    "utility_todo_add": [
        {"name": "title", "key": 1, "type": "string", "default": None},
        {"name": "content", "key": 2, "type": "string", "default": ""},
        {"name": "deadline", "key": 3, "type": "string", "default": ""},
    ],
    "zone_list": [
        {"name": "mode", "key": 1, "type": "enum", "enum_ref": "work_mode", "default": "active"},
        {"name": "offset", "key": 2, "type": "integer", "default": 0},
        {"name": "limit", "key": 3, "type": "integer", "default": 10},
    ],
    "zone_delete": [
        {"name": "items", "key": 1, "type": "array", "item_fields": [{"name": "zone_id", "key": 1, "type": "integer"}, {"name": "mode", "key": 2, "type": "enum", "enum_ref": "work_mode"}]},
    ],
    "zone_add": [
        {"name": "zone_name", "key": 1, "type": "string", "default": None},
        {"name": "mode", "key": 2, "type": "enum", "enum_ref": "work_mode", "default": "active"},
    ],
    "zone_channel_list": [
        {"name": "mode", "key": 1, "type": "enum", "enum_ref": "work_mode", "default": "active"},
        {"name": "zone_id", "key": 2, "type": "integer", "default": 0},
        {"name": "offset", "key": 3, "type": "integer", "default": 0},
        {"name": "limit", "key": 4, "type": "integer", "default": 10},
    ],
    "zone_channel_active_get": [
        {"name": "mode", "key": 1, "type": "enum", "enum_ref": "work_mode", "default": "active"},
    ],
    "zone_channel_active_set": [
        {"name": "mode", "key": 1, "type": "enum", "enum_ref": "work_mode", "default": "active"},
        {"name": "zone_id", "key": 2, "type": "integer", "default": None},
        {"name": "channel_id", "key": 3, "type": "integer", "default": None},
    ],
    "zone_channel_active_index_set": [
        {"name": "zone_index", "key": 1, "type": "integer", "default": None},
        {"name": "channel_index", "key": 2, "type": "integer", "default": None},
        {"name": "mode", "key": 3, "type": "enum", "enum_ref": "work_mode", "default": "active"},
    ],
}

TOOL_NAME_TO_RESPONSE_SCHEMA = {
    "accessory_bt_list": [
        {"name": "code", "key": 1, "type": "integer", "presence": "mandatory"},
        {"name": "total", "key": 2, "type": "integer", "presence": "mandatory"},
        {"name": "has_more", "key": 3, "type": "boolean", "presence": "mandatory"},
        {"name": "items", "key": 4, "type": "array", "item_fields": [{"name": "address", "key": 1, "type": "string", "presence": "mandatory"}, {"name": "name", "key": 2, "type": "string", "presence": "mandatory"}, {"name": "rssi", "key": 3, "type": "integer", "presence": "optional"}, {"name": "paired", "key": 4, "type": "boolean", "presence": "optional"}, {"name": "connected", "key": 5, "type": "boolean", "presence": "optional"}]},
    ],
    "accessory_bt_pair": [
        {"name": "code", "key": 1, "type": "integer", "presence": "mandatory"},
    ],
    "accessory_bt_switch": [
        {"name": "code", "key": 1, "type": "integer", "presence": "mandatory"},
    ],
    "audio_microphone_switch": [
        {"name": "code", "key": 1, "type": "integer", "presence": "mandatory"},
    ],
    "audio_volume_get": [
        {"name": "code", "key": 1, "type": "integer", "presence": "mandatory"},
        {"name": "value", "key": 2, "type": "integer", "presence": "mandatory"},
        {"name": "type", "key": 3, "type": "enum", "enum_ref": "volume_type", "presence": "mandatory"},
        {"name": "step", "key": 4, "type": "integer", "presence": "optional"},
    ],
    "audio_volume_set": [
        {"name": "code", "key": 1, "type": "integer", "presence": "mandatory"},
    ],
    "call_answer": [
        {"name": "code", "key": 1, "type": "integer", "presence": "mandatory"},
    ],
    "call_end": [
        {"name": "code", "key": 1, "type": "integer", "presence": "mandatory"},
    ],
    "call_start": [
        {"name": "code", "key": 1, "type": "integer", "presence": "mandatory"},
    ],
    "contact_add": [
        {"name": "code", "key": 1, "type": "integer", "presence": "mandatory"},
    ],
    "contact_delete": [
        {"name": "code", "key": 1, "type": "integer", "presence": "mandatory"},
    ],
    "contact_update": [
        {"name": "code", "key": 1, "type": "integer", "presence": "mandatory"},
    ],
    "contact_list": [
        {"name": "code", "key": 1, "type": "integer", "presence": "mandatory"},
        {"name": "total", "key": 2, "type": "integer", "presence": "mandatory"},
        {"name": "has_more", "key": 3, "type": "boolean", "presence": "mandatory"},
        {"name": "items", "key": 4, "type": "array", "item_fields": [{"name": "contact_id", "key": 1, "type": "string", "presence": "mandatory"}, {"name": "name", "key": 2, "type": "string", "presence": "mandatory"}, {"name": "type", "key": 3, "type": "enum", "enum_ref": "target_type", "presence": "mandatory"}, {"name": "is_favorite", "key": 4, "type": "boolean", "presence": "optional"}]},
    ],
    "folder_group_active_get": [
        {"name": "code", "key": 1, "type": "integer", "presence": "mandatory"},
        {"name": "folder_id", "key": 2, "type": "integer", "presence": "mandatory"},
        {"name": "folder_name", "key": 3, "type": "string", "presence": "mandatory"},
        {"name": "group_id", "key": 4, "type": "integer", "presence": "mandatory"},
        {"name": "group_name", "key": 5, "type": "string", "presence": "mandatory"},
    ],
    "folder_group_active_set": [
        {"name": "code", "key": 1, "type": "integer", "presence": "mandatory"},
    ],
    "folder_group_active_index_set": [
        {"name": "code", "key": 1, "type": "integer", "presence": "mandatory"},
    ],
    "hmi_dialog_confirm": [
        {"name": "code", "key": 1, "type": "integer", "presence": "mandatory"},
    ],
    "hmi_profile_get": [
        {"name": "code", "key": 1, "type": "integer", "presence": "mandatory"},
        {"name": "profile", "key": 2, "type": "enum", "enum_ref": "hmi_profile", "presence": "mandatory"},
    ],
    "hmi_profile_set": [
        {"name": "code", "key": 1, "type": "integer", "presence": "mandatory"},
    ],
    "hmi_vibration_switch": [
        {"name": "code", "key": 1, "type": "integer", "presence": "mandatory"},
    ],
    "location_gps_switch": [
        {"name": "code", "key": 1, "type": "integer", "presence": "mandatory"},
    ],
    "location_gps_get": [
        {"name": "code", "key": 1, "type": "integer", "presence": "mandatory"},
        {"name": "value", "key": 2, "type": "string", "presence": "mandatory"},
        {"name": "latitude", "key": 3, "type": "float", "presence": "optional"},
        {"name": "longitude", "key": 4, "type": "float", "presence": "optional"},
    ],
    "location_report_trigger": [
        {"name": "code", "key": 1, "type": "integer", "presence": "mandatory"},
    ],
    "media_record_play": [
        {"name": "code", "key": 1, "type": "integer", "presence": "mandatory"},
    ],
    "power_battery_get": [
        {"name": "code", "key": 1, "type": "integer", "presence": "mandatory"},
        {"name": "value", "key": 2, "type": "integer", "presence": "mandatory"},
        {"name": "charging", "key": 3, "type": "boolean", "presence": "optional"},
        {"name": "health", "key": 4, "type": "integer", "presence": "optional"},
        {"name": "voltage", "key": 5, "type": "float", "presence": "optional"},
        {"name": "temperature", "key": 6, "type": "float", "presence": "optional"},
    ],
    "power_transmit_level_set": [
        {"name": "code", "key": 1, "type": "integer", "presence": "mandatory"},
    ],
    "radio_alias_get": [
        {"name": "code", "key": 1, "type": "integer", "presence": "mandatory"},
        {"name": "alias", "key": 2, "type": "string", "presence": "mandatory"},
    ],
    "radio_alias_set": [
        {"name": "code", "key": 1, "type": "integer", "presence": "mandatory"},
    ],
    "radio_id_get": [
        {"name": "code", "key": 1, "type": "integer", "presence": "mandatory"},
        {"name": "radio_id", "key": 2, "type": "string", "presence": "mandatory"},
        {"name": "mode", "key": 3, "type": "enum", "enum_ref": "work_mode", "presence": "mandatory"},
    ],
    "radio_id_set": [
        {"name": "code", "key": 1, "type": "integer", "presence": "mandatory"},
    ],
    "radio_time_get": [
        {"name": "code", "key": 1, "type": "integer", "presence": "mandatory"},
        {"name": "time", "key": 2, "type": "string", "presence": "mandatory"},
    ],
    "radio_version_get": [
        {"name": "code", "key": 1, "type": "integer", "presence": "mandatory"},
        {"name": "firmware", "key": 2, "type": "string", "presence": "mandatory"},
        {"name": "data", "key": 3, "type": "string", "presence": "mandatory"},
    ],
    "radio_find": [
        {"name": "code", "key": 1, "type": "integer", "presence": "mandatory"},
    ],
    "radio_interconnect_switch": [
        {"name": "code", "key": 1, "type": "integer", "presence": "mandatory"},
    ],
    "security_alert_start": [
        {"name": "code", "key": 1, "type": "integer", "presence": "mandatory"},
    ],
    "security_alert_end": [
        {"name": "code", "key": 1, "type": "integer", "presence": "mandatory"},
    ],
    "sms_send": [
        {"name": "code", "key": 1, "type": "integer", "presence": "mandatory"},
    ],
    "sms_location_send": [
        {"name": "code", "key": 1, "type": "integer", "presence": "mandatory"},
    ],
    "sms_play": [
        {"name": "code", "key": 1, "type": "integer", "presence": "mandatory"},
    ],
    "sms_speak": [
        {"name": "code", "key": 1, "type": "integer", "presence": "mandatory"},
    ],
    "system_event_subscribe": [
        {"name": "code", "key": 1, "type": "integer", "presence": "mandatory"},
    ],
    "system_event_unsubscribe": [
        {"name": "code", "key": 1, "type": "integer", "presence": "mandatory"},
    ],
    "utility_todo_add": [
        {"name": "code", "key": 1, "type": "integer", "presence": "mandatory"},
    ],
    "zone_list": [
        {"name": "code", "key": 1, "type": "integer", "presence": "mandatory"},
        {"name": "total", "key": 2, "type": "integer", "presence": "mandatory"},
        {"name": "has_more", "key": 3, "type": "boolean", "presence": "mandatory"},
        {"name": "items", "key": 4, "type": "array", "item_fields": [{"name": "zone_id", "key": 1, "type": "integer", "presence": "mandatory"}, {"name": "zone_name", "key": 2, "type": "string", "presence": "mandatory"}]},
    ],
    "zone_delete": [
        {"name": "code", "key": 1, "type": "integer", "presence": "mandatory"},
    ],
    "zone_add": [
        {"name": "code", "key": 1, "type": "integer", "presence": "mandatory"},
        {"name": "zone_id", "key": 2, "type": "integer", "presence": "mandatory"},
    ],
    "zone_channel_list": [
        {"name": "code", "key": 1, "type": "integer", "presence": "mandatory"},
        {"name": "total", "key": 2, "type": "integer", "presence": "mandatory"},
        {"name": "has_more", "key": 3, "type": "boolean", "presence": "mandatory"},
        {"name": "items", "key": 4, "type": "array", "item_fields": [{"name": "channel_id", "key": 1, "type": "integer", "presence": "mandatory"}, {"name": "channel_name", "key": 2, "type": "string", "presence": "mandatory"}, {"name": "channel_type", "key": 3, "type": "enum", "enum_ref": "channel_type", "presence": "mandatory"}]},
    ],
    "zone_channel_active_get": [
        {"name": "code", "key": 1, "type": "integer", "presence": "mandatory"},
        {"name": "zone_id", "key": 2, "type": "integer", "presence": "mandatory"},
        {"name": "zone_name", "key": 3, "type": "string", "presence": "mandatory"},
        {"name": "channel_id", "key": 4, "type": "integer", "presence": "mandatory"},
        {"name": "channel_name", "key": 5, "type": "string", "presence": "mandatory"},
    ],
    "zone_channel_active_set": [
        {"name": "code", "key": 1, "type": "integer", "presence": "mandatory"},
    ],
    "zone_channel_active_index_set": [
        {"name": "code", "key": 1, "type": "integer", "presence": "mandatory"},
    ],
}

TOOL_ID_TO_REQUEST_SCHEMA = {
    1: TOOL_NAME_TO_REQUEST_SCHEMA["accessory_bt_list"],
    2: TOOL_NAME_TO_REQUEST_SCHEMA["accessory_bt_pair"],
    3: TOOL_NAME_TO_REQUEST_SCHEMA["accessory_bt_switch"],
    4: TOOL_NAME_TO_REQUEST_SCHEMA["audio_microphone_switch"],
    5: TOOL_NAME_TO_REQUEST_SCHEMA["audio_volume_get"],
    6: TOOL_NAME_TO_REQUEST_SCHEMA["audio_volume_set"],
    7: TOOL_NAME_TO_REQUEST_SCHEMA["call_answer"],
    8: TOOL_NAME_TO_REQUEST_SCHEMA["call_end"],
    9: TOOL_NAME_TO_REQUEST_SCHEMA["call_start"],
    10: TOOL_NAME_TO_REQUEST_SCHEMA["contact_add"],
    11: TOOL_NAME_TO_REQUEST_SCHEMA["contact_delete"],
    12: TOOL_NAME_TO_REQUEST_SCHEMA["contact_update"],
    13: TOOL_NAME_TO_REQUEST_SCHEMA["contact_list"],
    14: TOOL_NAME_TO_REQUEST_SCHEMA["folder_group_active_get"],
    15: TOOL_NAME_TO_REQUEST_SCHEMA["folder_group_active_set"],
    16: TOOL_NAME_TO_REQUEST_SCHEMA["folder_group_active_index_set"],
    17: TOOL_NAME_TO_REQUEST_SCHEMA["hmi_dialog_confirm"],
    18: TOOL_NAME_TO_REQUEST_SCHEMA["hmi_profile_get"],
    19: TOOL_NAME_TO_REQUEST_SCHEMA["hmi_profile_set"],
    20: TOOL_NAME_TO_REQUEST_SCHEMA["hmi_vibration_switch"],
    21: TOOL_NAME_TO_REQUEST_SCHEMA["location_gps_switch"],
    22: TOOL_NAME_TO_REQUEST_SCHEMA["location_gps_get"],
    23: TOOL_NAME_TO_REQUEST_SCHEMA["location_report_trigger"],
    24: TOOL_NAME_TO_REQUEST_SCHEMA["media_record_play"],
    25: TOOL_NAME_TO_REQUEST_SCHEMA["power_battery_get"],
    26: TOOL_NAME_TO_REQUEST_SCHEMA["power_transmit_level_set"],
    27: TOOL_NAME_TO_REQUEST_SCHEMA["radio_alias_get"],
    28: TOOL_NAME_TO_REQUEST_SCHEMA["radio_alias_set"],
    29: TOOL_NAME_TO_REQUEST_SCHEMA["radio_id_get"],
    30: TOOL_NAME_TO_REQUEST_SCHEMA["radio_id_set"],
    31: TOOL_NAME_TO_REQUEST_SCHEMA["radio_time_get"],
    32: TOOL_NAME_TO_REQUEST_SCHEMA["radio_version_get"],
    33: TOOL_NAME_TO_REQUEST_SCHEMA["radio_find"],
    34: TOOL_NAME_TO_REQUEST_SCHEMA["radio_interconnect_switch"],
    35: TOOL_NAME_TO_REQUEST_SCHEMA["security_alert_start"],
    36: TOOL_NAME_TO_REQUEST_SCHEMA["security_alert_end"],
    37: TOOL_NAME_TO_REQUEST_SCHEMA["sms_send"],
    38: TOOL_NAME_TO_REQUEST_SCHEMA["sms_location_send"],
    39: TOOL_NAME_TO_REQUEST_SCHEMA["sms_play"],
    40: TOOL_NAME_TO_REQUEST_SCHEMA["sms_speak"],
    41: TOOL_NAME_TO_REQUEST_SCHEMA["system_event_subscribe"],
    42: TOOL_NAME_TO_REQUEST_SCHEMA["system_event_unsubscribe"],
    43: TOOL_NAME_TO_REQUEST_SCHEMA["utility_todo_add"],
    44: TOOL_NAME_TO_REQUEST_SCHEMA["zone_list"],
    45: TOOL_NAME_TO_REQUEST_SCHEMA["zone_delete"],
    46: TOOL_NAME_TO_REQUEST_SCHEMA["zone_add"],
    47: TOOL_NAME_TO_REQUEST_SCHEMA["zone_channel_list"],
    48: TOOL_NAME_TO_REQUEST_SCHEMA["zone_channel_active_get"],
    49: TOOL_NAME_TO_REQUEST_SCHEMA["zone_channel_active_set"],
    50: TOOL_NAME_TO_REQUEST_SCHEMA["zone_channel_active_index_set"],
}

TOOL_ID_TO_RESPONSE_SCHEMA = {
    1: TOOL_NAME_TO_RESPONSE_SCHEMA["accessory_bt_list"],
    2: TOOL_NAME_TO_RESPONSE_SCHEMA["accessory_bt_pair"],
    3: TOOL_NAME_TO_RESPONSE_SCHEMA["accessory_bt_switch"],
    4: TOOL_NAME_TO_RESPONSE_SCHEMA["audio_microphone_switch"],
    5: TOOL_NAME_TO_RESPONSE_SCHEMA["audio_volume_get"],
    6: TOOL_NAME_TO_RESPONSE_SCHEMA["audio_volume_set"],
    7: TOOL_NAME_TO_RESPONSE_SCHEMA["call_answer"],
    8: TOOL_NAME_TO_RESPONSE_SCHEMA["call_end"],
    9: TOOL_NAME_TO_RESPONSE_SCHEMA["call_start"],
    10: TOOL_NAME_TO_RESPONSE_SCHEMA["contact_add"],
    11: TOOL_NAME_TO_RESPONSE_SCHEMA["contact_delete"],
    12: TOOL_NAME_TO_RESPONSE_SCHEMA["contact_update"],
    13: TOOL_NAME_TO_RESPONSE_SCHEMA["contact_list"],
    14: TOOL_NAME_TO_RESPONSE_SCHEMA["folder_group_active_get"],
    15: TOOL_NAME_TO_RESPONSE_SCHEMA["folder_group_active_set"],
    16: TOOL_NAME_TO_RESPONSE_SCHEMA["folder_group_active_index_set"],
    17: TOOL_NAME_TO_RESPONSE_SCHEMA["hmi_dialog_confirm"],
    18: TOOL_NAME_TO_RESPONSE_SCHEMA["hmi_profile_get"],
    19: TOOL_NAME_TO_RESPONSE_SCHEMA["hmi_profile_set"],
    20: TOOL_NAME_TO_RESPONSE_SCHEMA["hmi_vibration_switch"],
    21: TOOL_NAME_TO_RESPONSE_SCHEMA["location_gps_switch"],
    22: TOOL_NAME_TO_RESPONSE_SCHEMA["location_gps_get"],
    23: TOOL_NAME_TO_RESPONSE_SCHEMA["location_report_trigger"],
    24: TOOL_NAME_TO_RESPONSE_SCHEMA["media_record_play"],
    25: TOOL_NAME_TO_RESPONSE_SCHEMA["power_battery_get"],
    26: TOOL_NAME_TO_RESPONSE_SCHEMA["power_transmit_level_set"],
    27: TOOL_NAME_TO_RESPONSE_SCHEMA["radio_alias_get"],
    28: TOOL_NAME_TO_RESPONSE_SCHEMA["radio_alias_set"],
    29: TOOL_NAME_TO_RESPONSE_SCHEMA["radio_id_get"],
    30: TOOL_NAME_TO_RESPONSE_SCHEMA["radio_id_set"],
    31: TOOL_NAME_TO_RESPONSE_SCHEMA["radio_time_get"],
    32: TOOL_NAME_TO_RESPONSE_SCHEMA["radio_version_get"],
    33: TOOL_NAME_TO_RESPONSE_SCHEMA["radio_find"],
    34: TOOL_NAME_TO_RESPONSE_SCHEMA["radio_interconnect_switch"],
    35: TOOL_NAME_TO_RESPONSE_SCHEMA["security_alert_start"],
    36: TOOL_NAME_TO_RESPONSE_SCHEMA["security_alert_end"],
    37: TOOL_NAME_TO_RESPONSE_SCHEMA["sms_send"],
    38: TOOL_NAME_TO_RESPONSE_SCHEMA["sms_location_send"],
    39: TOOL_NAME_TO_RESPONSE_SCHEMA["sms_play"],
    40: TOOL_NAME_TO_RESPONSE_SCHEMA["sms_speak"],
    41: TOOL_NAME_TO_RESPONSE_SCHEMA["system_event_subscribe"],
    42: TOOL_NAME_TO_RESPONSE_SCHEMA["system_event_unsubscribe"],
    43: TOOL_NAME_TO_RESPONSE_SCHEMA["utility_todo_add"],
    44: TOOL_NAME_TO_RESPONSE_SCHEMA["zone_list"],
    45: TOOL_NAME_TO_RESPONSE_SCHEMA["zone_delete"],
    46: TOOL_NAME_TO_RESPONSE_SCHEMA["zone_add"],
    47: TOOL_NAME_TO_RESPONSE_SCHEMA["zone_channel_list"],
    48: TOOL_NAME_TO_RESPONSE_SCHEMA["zone_channel_active_get"],
    49: TOOL_NAME_TO_RESPONSE_SCHEMA["zone_channel_active_set"],
    50: TOOL_NAME_TO_RESPONSE_SCHEMA["zone_channel_active_index_set"],
}

# Event registry: id + field schema (enum_ref based)
EVENT_REGISTRY = {
    "call_received": {"id": 1, "fields": [{"name": "target_type", "type": "string", "enum_ref": "target_type", "presence": "mandatory"}, {"name": "target_id", "type": "string", "presence": "mandatory"}, {"name": "caller_id", "type": "string", "presence": "mandatory"}, {"name": "priority", "type": "string", "enum_ref": "call_priority", "presence": "mandatory"}, {"name": "caller_alias", "type": "string", "presence": "optional"}]},
    "call_status_changed": {"id": 2, "fields": [{"name": "status", "type": "string", "enum_ref": "call_status", "presence": "mandatory"}, {"name": "talker_id", "type": "string", "presence": "optional"}]},
    "sms_received": {"id": 3, "fields": [{"name": "target_type", "type": "string", "enum_ref": "target_type", "presence": "mandatory"}, {"name": "target_id", "type": "string", "presence": "mandatory"}, {"name": "sender_id", "type": "string", "presence": "mandatory"}, {"name": "content", "type": "string", "presence": "mandatory"}]},
}