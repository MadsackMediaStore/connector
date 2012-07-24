# -*- encoding: utf-8 -*-
##############################################################################
#
#    OpenERP, Open Source Management Solution
#    Copyright (C) 2004-2009 Tiny SPRL (<http://tiny.be>). All Rights Reserved
#    Sharoon Thomas, Raphaël Valyi
#    Copyright (C) 2011-2012 Camptocamp Guewen Baconnier
#
#    This program is free software: you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation, either version 3 of the License, or
#    (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU General Public License
#    along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
##############################################################################

from osv import fields, osv
import base64
import time
import netsvc
from datetime import datetime
import logging
import pooler
from collections import defaultdict
from lxml import objectify
from message_error import MappingError, ExtConnError
from tools.translate import _
from tools import DEFAULT_SERVER_DATETIME_FORMAT, DEFAULT_SERVER_DATE_FORMAT
#TODO fix me import do not work
#from base_external_referentials.decorator import commit_now

def extend(class_to_extend):
    def decorator(func):
        if hasattr(class_to_extend, func.func_name):
            raise osv.except_osv(_("Developper Error"),
                _("You can extend the class %s with the method %s.",
                "Indeed this method already exist use the decorator 'replace' instead"))
        setattr(class_to_extend, func.func_name, func)
        return class_to_extend
    return decorator


#TODO finish me Work in progress
def overwrite(class_to_extend):
    def decorator(func):
        original_func = hasattr(class_to_extend, func.func_name)
        if not original_func:
            raise osv.except_osv(_("Developper Error"),
                _("You can replace the method %s of the class %s.",
                "Indeed this method doesn't exist"))
        func.original_func = original_func
        setattr(class_to_extend, func.func_name, func)
        return class_to_extend
    return decorator



class ExternalSession(object):
    def __init__(self, referential, sync_from_object=None):
        self.referential_id = referential
        self.sync_from_object = sync_from_object
        self.debug = referential.debug
        self.logger = logging.getLogger(referential.name)
        self.connection = referential.external_connection(debug=self.debug, logger = self.logger)
        self.tmp = {}

    def is_type(self, referential_type):
        return self.referential_id.type_id.name.lower() == referential_type.lower()

    def is_categ(self, referential_category):
        return self.referential_id.categ_id.name.lower() == referential_category.lower()


#TODO think about the generic method to use
class Resource(object):

    def __init__(self, data):
        self.data = data

    def get(self, key):
        if isinstance(self.data, objectify.ObjectifiedElement):
            if key in self.data.__dict__:
                result = self.data.__dict__.get(key)
            else:
                return None
            if hasattr(result, 'pyval'):
                return result.pyval
            else:
                return Resource(result)

    def __getitem__(self, key):
        if isinstance(self.data, objectify.ObjectifiedElement):
            return self.get(key)

    def keys(self):
        if isinstance(self.data, objectify.ObjectifiedElement):
            return self.data.__dict__.keys()



########################################################################################################################
#
#                                             BASIC FEATURES
#
########################################################################################################################

@extend(osv.osv)
def read_w_order(self, cr, uid, ids, fields_to_read=None, context=None, load='_classic_read'):
    """ Read records with given ids with the given fields and return it respecting the order of the ids
    This is very usefull for synchronizing data in a special order with an external system

    :param list ids: list of the ids of the records to read
    :param list fields: optional list of field names to return (default: all fields would be returned)
    :param dict context: context arguments, like lang, time zone
    :return: ordered list of dictionaries((dictionary per record asked)) with requested field values
    :rtype: [{‘name_of_the_field’: value, ...}, ...]
    """
    res = self.read(cr, uid, ids, fields_to_read, context, load)
    resultat = []
    for id in ids:
        resultat += [x for x in res if x['id'] == id]
    return resultat

@extend(osv.osv)
def browse_w_order(self, cr, uid, ids, context=None, list_class=None, fields_process={}):
    """Fetch records as objects and return it respecting the order of the ids
    This is very usefull for synchronizing data in a special order with an external system

    :param list ids: id or list of ids.
    :param dict context: context arguments, like lang, time zone
    :return: ordered list of object
    :rtype: list of objects requested
    """
    res = self.browse(cr, uid, ids, context, list_class, fields_process)
    resultat = []
    for id in ids:
        resultat += [x for x in res if x.id == id]
    return resultat

@extend(osv.osv)
def prefixed_id(self, id):
    """The reason why we don't just use the external id and put the model as the prefix is to avoid unique ir_model_data#name per module constraint violation."""
    return self._name.replace('.', '_') + '/' + str(id)

@extend(osv.osv)
def id_from_prefixed_id(self, prefixed_id):
    res = prefixed_id.split(self._name.replace('.', '_') + '/')[1]
    if res.isdigit():
        return int(res)
    else:
        return res

@extend(osv.osv)
def get_all_extid_from_referential(self, cr, uid, referential_id, context=None):
    """Returns the external ids of the ressource which have an ext_id in the referential"""
    ir_model_data_obj = self.pool.get('ir.model.data')
    model_data_ids = ir_model_data_obj.search(cr, uid, [('model', '=', self._name), ('referential_id', '=', referential_id)])
    #because OpenERP might keep ir_model_data (is it a bug?) for deleted records, we check if record exists:
    oeid_to_extid = {}
    for data in ir_model_data_obj.read(cr, uid, model_data_ids, ['res_id', 'name'], context=context):
        oeid_to_extid[data['res_id']] = self.id_from_prefixed_id(data['name'])
    if not oeid_to_extid:
        return []
    return [int(oeid_to_extid[oe_id]) for oe_id in self.exists(cr, uid, oeid_to_extid.keys(), context=context)]

@extend(osv.osv)
def get_all_oeid_from_referential(self, cr, uid, referential_id, context=None):
    """Returns the openerp ids of the ressource which have an ext_id in the referential"""
    ir_model_data_obj = self.pool.get('ir.model.data')
    model_data_ids = ir_model_data_obj.search(cr, uid, [('model', '=', self._name), ('referential_id', '=', referential_id)])
    #because OpenERP might keep ir_model_data (is it a bug?) for deleted records, we check if record exists:
    claimed_oe_ids = [x['res_id'] for x in ir_model_data_obj.read(cr, uid, model_data_ids, ['res_id'], context=context)]
    return claimed_oe_ids and self.exists(cr, uid, claimed_oe_ids, context=context) or []

@extend(osv.osv)
def get_or_create_extid(self, cr, uid, external_session, openerp_id, context=None):
    """Returns the external id of a resource by its OpenERP id.
    Returns False if the resource id does not exists."""
    res = self.get_extid(cr, uid, openerp_id, external_session.referential_id.id, context=context)
    if res is not False:
        return res
    else:
        return self._export_one_resource(cr, uid, external_session, openerp_id, context=context)

@extend(osv.osv)
def get_extid(self, cr, uid, openerp_id, referential_id, context=None):
    """Returns the external id of a resource by its OpenERP id.
    Returns False if the resource id does not exists."""
    if isinstance(openerp_id, list):
        openerp_id = openerp_id[0]
    model_data_ids = self.pool.get('ir.model.data').search(cr, uid, [('model', '=', self._name), ('res_id', '=', openerp_id), ('referential_id', '=', referential_id)])
    if model_data_ids and len(model_data_ids) > 0:
        prefixed_id = self.pool.get('ir.model.data').read(cr, uid, model_data_ids[0], ['name'])['name']
        ext_id = self.id_from_prefixed_id(prefixed_id)
        return ext_id
    return False

#TODO Deprecated remove for V7 version
@extend(osv.osv)
def oeid_to_existing_extid(self, cr, uid, referential_id, openerp_id, context=None):
    """Returns the external id of a resource by its OpenERP id.
    Returns False if the resource id does not exists."""
    return self.get_extid(cr, uid, openerp_id, referential_id, context=context)

osv.osv.oeid_to_extid = osv.osv.get_or_create_extid
############## END OF DEPRECATED


@extend(osv.osv)
def _get_expected_oeid(self, cr, uid, external_id, referential_id, context=None):
    """
    Returns the id of the entry in ir.model.data and the expected id of the resource in the current model
    Warning the expected_oe_id may not exists in the model, that's the res_id registered in ir.model.data

    @param external_id: id in the external referential
    @param referential_id: id of the external referential
    @return: tuple of (ir.model.data entry id, expected resource id in the current model)
    """
    model_data_obj = self.pool.get('ir.model.data')
    model_data_ids = model_data_obj.search(cr, uid,
        [('name', '=', self.prefixed_id(external_id)),
         ('model', '=', self._name),
         ('referential_id', '=', referential_id)], context=context)
    model_data_id = model_data_ids and model_data_ids[0] or False
    expected_oe_id = False
    if model_data_id:
        expected_oe_id = model_data_obj.read(cr, uid, model_data_id, ['res_id'])['res_id']
    return model_data_id, expected_oe_id

@extend(osv.osv)
def get_oeid(self, cr, uid, external_id, referential_id, context=None):
    """Returns the OpenERP id of a resource by its external id.
       Returns False if the resource does not exist."""
    if external_id:
        ir_model_data_id, expected_oe_id = self._get_expected_oeid\
            (cr, uid, external_id, referential_id, context=context)
        # Note: OpenERP cleans up ir_model_data which res_id records have been deleted
        # only at server update because that would be a perf penalty, we returns the res_id only if
        # really existing and we delete the ir_model_data unused
        if expected_oe_id and self.exists(cr, uid, expected_oe_id, context=context):
            return expected_oe_id
    return False

@extend(osv.osv)
def get_or_create_oeid(self, cr, uid, external_session, external_id, context=None):
    """Returns the OpenERP ID of a resource by its external id.
    Creates the resource from the external connection if the resource does not exist."""
    if external_id:
        existing_id = self.get_oeid(cr, uid, external_id, external_session.referential_id.id, context=context)
        if existing_id:
            return existing_id
        return self._import_one_resource(cr, uid, external_session, external_id, context=context)
    return False

#TODO Deprecated remove for V7 version
@extend(osv.osv)
def extid_to_existing_oeid(self, cr, uid, referential_id, external_id, context=None):
    """Returns the OpenERP id of a resource by its external id.
       Returns False if the resource does not exist."""
    res = self.get_oeid(cr, uid, external_id, referential_id, context=context)
    return res

osv.osv.extid_to_oeid = osv.osv.get_or_create_oeid
############## END OF DEPRECATED


########################################################################################################################
#
#                                             END OF BASIC FEATURES
#
########################################################################################################################





########################################################################################################################
#
#                                             IMPORT FEATURES
#
########################################################################################################################


@extend(osv.osv)
def _get_filter(self, cr, uid, external_session, step, previous_filter=None, context=None):
    """
    Abstract function that return the filter
    Can be overwriten in your module

    :param ExternalSession external_session : External_session that contain all params of connection
    :param int step: Step the of the import, 100 meant you will import data per 100
    :param dict previous_filter: the previous filter
    :return: dictionary with a filter
    :rtype: dict
    """
    return None

@extend(osv.osv)
def _get_external_resource_ids(self, cr, uid, external_session, resource_filter=None, mapping=None, context=None):
    """
    Abstract function that return the external resource ids
    Can be overwriten in your module

    :param ExternalSession external_session : External_session that contain all params of connection
    :param dict resource_filter: the filter to apply to the external search method
    :param dict mapping: dictionnary of mapping, the key is the openerp object's name
    :return: a list of external_id
    :rtype: list
    """
    raise osv.except_osv(_("Not Implemented"), _("The method _get_external_resource_ids is not implemented in abstract base module!"))

@extend(osv.osv)
def _get_default_import_values(self, cr, uid, external_session, mapping_id=None, defaults=None, context=None):
    """
    Abstract function that return the default value for on object
    Can be overwriten in your module

    :param ExternalSession external_session : External_session that contain all params of connection
    :return: a dictionnary of default value
    :rtype: dict
    """
    return defaults

@extend(osv.osv)
def _get_import_step(self, cr, uid, external_session, context=None):
    """
    Abstract function that return the step for importing data
    Can be overwriten in your module

    :param ExternalSession external_session : External_session that contain all params of connection
    :return: a integer that correspond to the limit of object to import
    :rtype: int
    """
    return 100

@extend(osv.osv)
def _get_external_resources(self, cr, uid, external_session, external_id=None, resource_filter=None, mapping=None, fields=None, context=None):
    """
    Abstract function that return the external resource
    Can be overwriten in your module

    :param ExternalSession external_session : External_session that contain all params of connection
    :param int external_id : resource external id to import
    :param dict resource_filter: the filter to apply to the external search method
    :param dict mapping: dictionnary of mapping, the key is the openerp object's name
    :param list fields: list of field to read
    :return: a list of dict that contain resource information
    :rtype: list
    """
    mapping, mapping_id = self._init_mapping(cr, uid, external_session.referential_id.id, mapping=mapping, context=context)
    if not resource_filter: resource_filter = {}
    if external_id: resource_filter[mapping[mapping_id]['key_for_external_id']] = external_id

    return getattr(external_session.connection, mapping[mapping_id]['external_get_method'])(mapping[mapping_id]['external_resource_name'], resource_filter)

@extend(osv.osv)
def _get_mapping_id(self, cr, uid, referential_id, context=None):
    """
    Function that return the mapping id for the corresponding object

    :params int referential_id: the referential id
    :return the id of the mapping
    :rtype integer
    """
    mapping_id = self.pool.get('external.mapping').search(cr, uid, [('model', '=', self._name), ('referential_id', '=', referential_id)], context=context)
    return mapping_id and mapping_id[0] or False

@extend(osv.osv)
def _init_mapping(self, cr, uid, referential_id, convertion_type='from_external_to_openerp', mapping_line_filter_ids=None, mapping=None, mapping_id=None, context=None):
    if not mapping:
        mapping={}
    if not mapping_id:
        mapping_id = self._get_mapping_id(cr, uid, referential_id, context=context)
    if not mapping.get(mapping_id):
        mapping[mapping_id] = self._get_mapping(cr, uid, referential_id, convertion_type=convertion_type, mapping_line_filter_ids=mapping_line_filter_ids, mapping_id=mapping_id, context=context)
    return mapping, mapping_id

@extend(osv.osv)
def _get_mapping(self, cr, uid, referential_id, convertion_type='from_external_to_openerp', mapping_line_filter_ids=None, mapping_id=None, context=None):
    """
    Function that return the mapping line for the corresponding object

    :param  int referential_id: the referential id
    :return: dictionary with the key "mapping_lines" and "key_for_external_id"
    :rtype: dict
    """
    if not mapping_id:
        mapping_id = self._get_mapping_id(cr, uid, referential_id, context=context)
    if not mapping_id:
        raise osv.except_osv(_('External Import Error'), _("The object %s doesn't have an external mapping" %self._name))
    else:
        #If a mapping exists for current model, search for mapping lines

        mapping_type = convertion_type == 'from_external_to_openerp' and 'in' or 'out'
        mapping_line_filter = [('mapping_id', '=', mapping_id),
                            ('type', 'in', ['in_out', mapping_type])]
        if mapping_line_filter_ids:
            mapping_line_filter += ['|', ('id', 'in', mapping_line_filter_ids), ('evaluation_type', '=', 'sub-mapping')]
        mapping_line_ids = self.pool.get('external.mapping.line').search(cr, uid, mapping_line_filter, context=context)
        if mapping_line_ids:
            mapping_lines = self.pool.get('external.mapping.line').read(cr, uid, mapping_line_ids, [], context=context)
        else:
            mapping_lines = []
        res = self.pool.get('external.mapping').read(cr, uid, mapping_id, context=context)
        alternative_key = [x['internal_field'] for x in mapping_lines if x['alternative_key']]
        res['alternative_keys'] = alternative_key or False
        res['key_for_external_id'] = res['key_for_external_id'] or 'id'
        res['mapping_lines'] = mapping_lines
        return res

@extend(osv.osv)
def import_resources(self, cr, uid, ids, resource_name, method="search_then_read", context=None):
    """
    Abstract function to import resources from a shop / a referential...

    :param list ids: list of id
    :param string ressource_name: the resource name to import
    :return: dictionary with the key "create_ids" and "write_ids" which containt the id created/written
    :rtype: dict
    """
    result = {"create_ids" : [], "write_ids" : []}
    for browse_record in self.browse(cr, uid, ids, context=context):
        if browse_record._name == 'external.referential':
            external_session = ExternalSession(browse_record, browse_record)
        else:
            if hasattr(browse_record, 'referential_id'):
                context['%s_id'%browse_record._name.replace('.', '_')] = browse_record.id
                external_session = ExternalSession(browse_record.referential_id, browse_record)
            else:
                raise osv.except_osv(_("Not Implemented"), _("The field referential_id doesn't exist on the object %s. Reporting system can not be used" %(browse_record._name,)))
        defaults = self.pool.get(resource_name)._get_default_import_values(cr, uid, external_session, context=context)
        res = self.pool.get(resource_name)._import_resources(cr, uid, external_session, defaults, method=method, context=context)
        for key in result:
            result[key].append(res.get(key, []))
    return result


@extend(osv.osv)
def _import_resources(self, cr, uid, external_session, defaults=None, method="search_then_read", context=None):
    """
    Abstract function to import resources form a specific object (like shop, referential...)

    :param ExternalSession external_session : External_session that contain all params of connection
    :param dict defaults: default value for the resource to create
    :param str method: method to use to import resource
    :return: dictionary with the key "create_ids" and "write_ids" which containt the id created/written
    :rtype: dict
    """
    external_session.logger.info("Start to import the ressource %s"%(self._name,))
    result = {"create_ids" : [], "write_ids" : []}
    mapping, mapping_id = self._init_mapping(cr, uid, external_session.referential_id.id, context=context)
    if mapping[mapping_id].get('mapping_lines'):
        step = self._get_import_step(cr, uid, external_session, context=context)
        resource_filter = None
        #TODO refactor improve and simplify this code
        if method == 'search_then_read':
            while True:
                resource_filter = self._get_filter(cr, uid, external_session, step, previous_filter=resource_filter, context=context)
                import pdb; pdb.set_trace()
                ext_ids = self._get_external_resource_ids(cr, uid, external_session, resource_filter, mapping=mapping, context=context)
                if not ext_ids:
                    break
                for ext_id in ext_ids:
                    #TODO import only the field needed to improve speed import ;)
                    resources = self._get_external_resources(cr, uid, external_session, ext_id, mapping=mapping, fields=None, context=context)
                    if not isinstance(resources, list):
                        resources = [resources]
                    res = self._record_external_resources(cr, uid, external_session, resources, defaults=defaults, mapping=mapping, mapping_id=mapping_id, context=context)
                    for key in result:
                        result[key].append(res.get(key, []))
        elif method == 'search_then_read_no_loop':
            #Magento API do not support step import so we can not use a loop
            resource_filter = self._get_filter(cr, uid, external_session, step, previous_filter=resource_filter, context=context)
            ext_ids = self._get_external_resource_ids(cr, uid, external_session, resource_filter, mapping=mapping, context=context)
            for ext_id in ext_ids:
                #TODO import only the field needed to improve speed import ;)
                resources = self._get_external_resources(cr, uid, external_session, ext_id, mapping=mapping, fields=None, context=context)
                if not isinstance(resources, list):
                    resources = [resources]
                res = self._record_external_resources(cr, uid, external_session, resources, defaults=defaults, mapping=mapping, mapping_id=mapping_id, context=context)
                for key in result:
                    result[key].append(res.get(key, []))
        elif method == 'search_read':
            while True:
                resource_filter = self._get_filter(cr, uid, external_session, step, previous_filter=resource_filter, context=context)
                #TODO import only the field needed to improve speed import ;)
                resources = self._get_external_resources(cr, uid, external_session, resource_filter=resource_filter, mapping=mapping, fields=None, context=context)
                if not resources:
                    break
                if not isinstance(resources, list):
                    resources = [resources]
                res = self._record_external_resources(cr, uid, external_session, resources, defaults=defaults, mapping=mapping, mapping_id=mapping_id, context=context)
                for key in result:
                    result[key].append(res.get(key, []))
        elif method == 'search_read_no_loop':
            #Magento API do not support step import so we can not use a loop
            resource_filter = self._get_filter(cr, uid, external_session, step, previous_filter=resource_filter, context=context)
            #TODO import only the field needed to improve speed import ;)
            resources = self._get_external_resources(cr, uid, external_session, resource_filter=resource_filter, mapping=mapping, fields=None, context=context)
            if not hasattr(resources, '__iter__'):
                resources = [resources]
            import pdb; pdb.set_trace()
            res = self._record_external_resources(cr, uid, external_session, resources, defaults=defaults, mapping=mapping, mapping_id=mapping_id, context=context)
            for key in result:
                result[key].append(res.get(key, []))
    return result

@extend(osv.osv)
def _import_one_resource(self, cr, uid, external_session, external_id, context=None):
    """
    Abstract function to import one resource

    :param ExternalSession external_session : External_session that contain all params of connection
    :param int external_id : resource external id to import
    :return: the openerp id of the resource imported
    :rtype: int
    """
    resources = self._get_external_resources(cr, uid, external_session, external_id, context=context)
    if isinstance(resources, list):
        res = self._record_external_resources(cr, uid, external_session, resources, context=context)
        id = res.get('write_ids') and res['write_ids'][0] or res['create_ids'][0]
    else:
        res = self._record_one_external_resource(cr, uid, external_session, resources, context=context)
        id = res.get('write_id') or res.get('create_id')
    return id

@extend(osv.osv)
def _record_external_resources(self, cr, uid, external_session, resources, defaults=None, mapping=None, mapping_id=None, context=None):
    """
    Abstract function to record external resources (this will convert the data and create/update the object in openerp)

    :param ExternalSession external_session : External_session that contain all params of connection
    :param list resource: list of resource to import
    :param dict defaults: default value for the resource to create
    :param dict mapping: dictionnary of mapping, the key is the openerp object's name
    :return: dictionary with the key "create_ids" and "write_ids" which containt the id created/written
    :rtype: dict
    """
    result = {'write_ids': [], 'create_ids': []}
    mapping, mapping_id = self._init_mapping(cr, uid, external_session.referential_id.id, mapping=mapping, mapping_id=mapping_id, context=context)
    if mapping[mapping_id]['key_for_external_id']:
        context['external_id_key_for_report'] = mapping[mapping_id]['key_for_external_id']
    else:
        for field in mapping[mapping_id]['mapping_lines']:
            if field['alternative_key']:
                context['external_id_key_for_report'] = field['external_field']
                break
    for resource in resources:
        res = self._record_one_external_resource(cr, uid, external_session, resource, defaults=defaults, mapping=mapping, mapping_id=mapping_id, context=context)
        if res:
            if res.get('create_id'): result['create_ids'].append(res['create_id'])
            if res.get('write_id'): result['write_ids'].append(res['write_id'])
    return result

@extend(osv.osv)
def _record_one_external_resource(self, cr, uid, external_session, resource, defaults=None, mapping=None, mapping_id=None, context=None):
    """
    Used in _record_external_resources
    The resource will converted into OpenERP data by using the function _transform_external_resources
    And then created or updated, and an external id will be added into the table ir.model.data

    :param dict resource: resource to convert into OpenERP data
    :param int referential_id: external referential id from where we import the resource
    :param dict defaults: defaults value
    :return: dictionary with the key "create_id" and "write_id" which containt the id created/written
    """
    mapping, mapping_id = self._init_mapping(cr, uid, external_session.referential_id.id, mapping=mapping, mapping_id=mapping_id, context=context)
    written = created = False
    vals = self._transform_one_resource(cr, uid, external_session, 'from_external_to_openerp', resource, mapping=mapping, mapping_id=mapping_id, defaults=defaults, context=context)
    if not vals:
        # for example in the case of an update on existing resource if update is not wanted vals will be {}
        return {}
    referential_id = external_session.referential_id.id
    external_id = vals.get('external_id')
    external_id_ok = not (external_id is None or external_id is False)
    alternative_keys = mapping[mapping_id]['alternative_keys']
    existing_rec_id = False
    existing_ir_model_data_id = False
    if external_id_ok:
        del vals['external_id']
    existing_ir_model_data_id, existing_rec_id = self._get_oeid_from_extid_or_alternative_keys\
            (cr, uid, vals, external_id, referential_id, alternative_keys, context=context)

    if not (external_id_ok or alternative_keys):
        external_session.logger.warning(_("The object imported need an external_id, maybe the mapping doesn't exist for the object : %s" %self._name))

    if existing_rec_id:
        if not self._name in context.get('do_not_update', []):
            if self.oe_update(cr, uid, external_session, existing_rec_id, vals, resource, defaults=defaults, context=context):
                written = True
    else:
        existing_rec_id = self.oe_create(cr, uid,  external_session, vals, resource, defaults, context=context)
        created = True

    if external_id_ok:
        if existing_ir_model_data_id:
            if created:
                # means the external ressource is registred in ir.model.data but the ressource doesn't exist
                # in this case we have to update the ir.model.data in order to point to the ressource created
                self.pool.get('ir.model.data').write(cr, uid, existing_ir_model_data_id, {'res_id': existing_rec_id}, context=context)
        else:
            ir_model_data_vals = \
            self.create_external_id_vals(cr, uid, existing_rec_id, external_id, referential_id, context=context)
            if not created:
                # means the external resource is bound to an already existing resource
                # but not registered in ir.model.data, we log it to inform the success of the binding
                external_session.logger.info("Bound in OpenERP %s from External Ref with "
                                            "external_id %s and OpenERP id %s successfully" %(self._name, external_id, existing_rec_id))

    if created:
        if external_id:
            external_session.logger.info(("Created in OpenERP %s from External Ref with"
                                    "external_id %s and OpenERP id %s successfully" %(self._name, external_id_ok and str(external_id), existing_rec_id)))
        elif alternative_keys:
            external_session.logger.info(("Created in OpenERP %s from External Ref with"
                                    "alternative_keys %s and OpenERP id %s successfully" %(self._name, external_id_ok and str (vals.get(alternative_keys)), existing_rec_id)))
        return {'create_id' : existing_rec_id}
    elif written:
        if external_id:
            external_session.logger.info(("Updated in OpenERP %s from External Ref with"
                                    "external_id %s and OpenERP id %s successfully" %(self._name, external_id_ok and str(external_id), existing_rec_id)))
        elif alternative_keys:
            external_session.logger.info(("Updated in OpenERP %s from External Ref with"
                                    "alternative_keys %s and OpenERP id %s successfully" %(self._name, external_id_ok and str (vals.get(alternative_keys)), existing_rec_id)))
        return {'write_id' : existing_rec_id}
    return {}

@extend(osv.osv)
def retry_import(self, cr, uid, id, ext_id, referential_id, defaults=None, context=None):
    """ When we import again a previously failed import
    """
    raise osv.except_osv(_("Not Implemented"), _("Not Implemented in abstract base module!"))

@extend(osv.osv)
def oe_update(self, cr, uid, external_session, existing_rec_id, vals, resource, defaults, context=None):
    if not context: context={}
    context['referential_id'] = external_session.referential_id.id #did it's needed somewhere?
    return self.write(cr, uid, existing_rec_id, vals, context)

@extend(osv.osv)
def oe_create(self, cr, uid, external_session, vals, resource, defaults, context=None):
    if not context: context={}
    context['referential_id'] = external_session.referential_id.id  #did it's needed somewhere?
    return self.create(cr, uid, vals, context)

########################################################################################################################
#
#                                             END OF IMPORT FEATURES
#
########################################################################################################################




########################################################################################################################
#
#                                             EXPORT FEATURES
#
########################################################################################################################

@extend(osv.osv)
def _get_export_step(self, cr, uid, external_session, context=None):
    """
    Abstract function that return the step for importing data
    Can be overwriten in your module

    :param ExternalSession external_session : External_session that contain all params of connection
    :return: a integer that corespond to the limit of object to import
    :rtype: int
    """
    return 10

@extend(osv.osv)
def _get_default_export_values(self, cr, uid, external_session, mapping_id=None, defaults=None, context=None):
    """
    Abstract function that return the default value for on object
    Can be overwriten in your module

    :param ExternalSession external_session : External_session that contain all params of connection
    :return: a dictionnary of default value
    :rtype: dict
    """
    return defaults

@extend(osv.osv)
def _get_last_exported_date(self, cr, uid, external_session, context=None):
    return False

@extend(osv.osv)
def _set_last_exported_date(self, cr, uid, external_session, date, context=None):
    return False

#For now it's just support 1 level of inherit TODO make it recursive
@extend(osv.osv)
def _get_query_and_params_for_ids_and_date(self, cr, uid, external_session, ids=None, last_exported_date=None, context=None):
    object_table = self._table
    params = ()
    if not self._inherits:
        greatest = "GREATEST(%(object_table)s.write_date, %(object_table)s.create_date)"\
                        %{'object_table': object_table}

        query = """
            SELECT %(greatest)s as update_date, %(object_table)s.id as id, ir_model_data.res_id
                FROM %(object_table)s
            LEFT JOIN ir_model_data
                ON %(object_table)s.id = ir_model_data.res_id
                AND ir_model_data.model = '%(object_name)s'
                AND ir_model_data.module = 'extref/%(ref_name)s'
            """%{
                    'greatest': greatest,
                    'object_table': object_table,
                    'object_name': self._name,
                    'ref_name': external_session.referential_id.name,
            }
    else:
        inherits_object_table = self.pool.get(self._inherits.keys()[0])._table
        join_field = self._inherits[self._inherits.keys()[0]]

        greatest = """GREATEST(%(object_table)s.write_date, %(object_table)s.create_date,
                    %(inherits_object_table)s.write_date, %(inherits_object_table)s.create_date)""" \
                    %{'object_table': object_table, 'inherits_object_table': inherits_object_table}

        query = """
            select %(greatest)s as update_date, %(object_table)s.id as id, ir_model_data.res_id
                from %(object_table)s
                    join %(inherits_object_table)s on %(inherits_object_table)s.id = %(object_table)s.%(join_field)s
                    LEFT JOIN ir_model_data
                        ON %(object_table)s.id = ir_model_data.res_id
                        AND ir_model_data.model = '%(object_name)s'
                        AND ir_model_data.module = 'extref/%(ref_name)s'
            """ %{
                    'greatest': greatest,
                    'object_table': object_table,
                    'inherits_object_table': inherits_object_table,
                    'join_field': join_field,
                    'object_name': self._name,
                    'ref_name': external_session.referential_id.name,
                }
    if ids:
        query += " WHERE " + object_table + ".id in %s"
        params += (tuple(ids),)
    if last_exported_date:
        query += (ids and " AND (" or " WHERE (") + greatest + " > %s or ir_model_data.res_id is NULL)"
        params += (last_exported_date,)

    query += " order by update_date asc;"
    return query, params

@extend(osv.osv)
def get_ids_and_update_date(self, cr, uid, external_session, ids=None, last_exported_date=None, context=None):
    query, params = self._get_query_and_params_for_ids_and_date(cr, uid, external_session, ids=ids, last_exported_date=last_exported_date, context=context)
    cr.execute(query, params)
    read = cr.dictfetchall()
    ids = []
    ids_2_dates = {}
    for data in read:
        ids.append(data['id'])
        ids_2_dates[data['id']] = data['update_date']
    return ids, ids_2_dates

@extend(osv.osv)
def init_context_before_exporting_resource(self, cr, uid, external_session, object_id, resource_name, context=None):
    if self._name != 'external.referential' and 'referential_id' in self._columns.keys():
        context['%s_id'%self._name.replace('.', '_')] = object_id
    return context

@extend(osv.osv)
def export_resources(self, cr, uid, ids, resource_name, context=None):
    """
    Abstract function to export resources from a shop / a referential...

    :param list ids: list of id
    :param string ressource_name: the resource name to import
    :return: True
    :rtype: boolean
    """
    for browse_record in self.browse(cr, uid, ids, context=context):
        if browse_record._name == 'external.referential':
            external_session = ExternalSession(browse_record, browse_record)
        else:
            if hasattr(browse_record, 'referential_id'):
                external_session = ExternalSession(browse_record.referential_id, browse_record)
            else:
                raise osv.except_osv(_("Not Implemented"), _("The field referential_id doesn't exist on the object %s." %(browse_record._name,)))
        context = self.init_context_before_exporting_resource(cr, uid, external_session, browse_record.id, resource_name, context=context)
        self.pool.get(resource_name)._export_resources(cr, uid, external_session, context=context)
    return True

@extend(osv.osv)
def send_to_external(self, cr, uid, external_session, resources, mapping, mapping_id, update_date=None, context=None):
    resources_to_update = {}
    resources_to_create = {}
    for resource_id, resource in resources.items():
        ext_id = self.get_extid(cr, uid, resource_id, external_session.referential_id.id, context=context)
        if ext_id:
            for lang in resource:
                resource[lang]['ext_id'] = ext_id
            resources_to_update[resource_id] = resource
        else:
            resources_to_create[resource_id] = resource
    self.ext_update(cr, uid, external_session, resources_to_update, mapping, mapping_id, context=context)
    ext_create_ids = self.ext_create(cr, uid, external_session, resources_to_create, mapping, mapping_id, context=context)
    for rec_id, ext_id in ext_create_ids.items():
        self.create_external_id_vals(cr, uid, rec_id, ext_id, external_session.referential_id.id, context=context)
    if update_date and self._get_last_exported_date(cr, uid, external_session, context=context) < update_date:
        self._set_last_exported_date(cr, uid, external_session, update_date, context=context)
    return ext_id

@extend(osv.osv)
def ext_create(self, cr, uid, external_session, resources, mapping=None, mapping_id=None, context=None):
    res = {}
    mapping, mapping_id = self._init_mapping(cr, uid, external_session.referential_id.id, mapping=mapping, mapping_id=mapping_id, context=context)
    for resource_id, resource in resources.items():
        # TODO support multilanguages. for now we only export the first one
        res[resource_id] = getattr(external_session.connection, mapping[mapping_id]['external_create_method'])(mapping[mapping_id]['external_resource_name'], resource[resource.keys()[0]])
    return res

@extend(osv.osv)
def ext_update(self, cr, uid, external_session, resources, mapping=None, mapping_id=None, context=None):
    """Not Implemented here"""
    return False

@extend(osv.osv)
def ext_unlink(self, cr, uid, ids, context=None):
    ir_model_obj = self.pool.get('ir.model.data')
    for object_id in ids:
        ir_model_ids = ir_model_obj.search(cr, uid, [('res_id','=',object_id),('model','=',self._name)])
        for ir_model in ir_model_obj.browse(cr, uid, ir_model_ids, context=context):
            ext_id = self.id_from_prefixed_id(ir_model.name)
            ref_id = ir_model.referential_id.id
            external_session = ExternalSession(ir_model.referential_id)
            mapping = self._get_mapping(cr, uid, ref_id)
            getattr(external_session.connection, mapping['external_delete_method'])(mapping['external_resource_name'], ext_id)
            #commit_now(ir_model.unlink())
            ir_model.unlink()
    return True

@extend(osv.osv)
def get_lang_to_export(self, cr, uid, external_session, context=None):
    if not context:
        return []
    else:
        return context.get('lang_to_export') or [context.get('lang')]

@extend(osv.osv)
def _export_resources(self, cr, uid, external_session, method="onebyone", context=None):
    external_session.logger.info("Start to export the ressource %s"%(self._name,))
    defaults = self._get_default_export_values(cr, uid, external_session, context=context)
    mapping, mapping_id = self._init_mapping(cr, uid, external_session.referential_id.id, convertion_type='from_openerp_to_external', context=context)
    last_exported_date = self._get_last_exported_date(cr, uid, external_session, context=context)
    external_session.logger.info("Retrieve the list of ids to export for the ressource %s"%(self._name))
    ids, ids_2_date = self.get_ids_and_update_date(cr, uid, external_session, last_exported_date=last_exported_date, context=context)
    external_session.logger.info("%s %s ressource will be exported"%(len(ids), self._name))
    step = self._get_export_step(cr, uid, external_session, context=context)

    group_obj = self.pool.get('group.fields')
    group_ids = group_obj.search(cr, uid, [['model_id', '=', self._name]], context=context)
    if self._inherits:
        inherits_group_ids = group_obj.search(cr, uid, [['model_id', '=',self._inherits.keys()[0]]], context=context)
    else:
        inherits_group_ids=[]
    smart_export =  context.get('smart_export') and (group_ids or inherits_group_ids) and {'group_ids': group_ids, 'inherits_group_ids': inherits_group_ids}

    langs = self.get_lang_to_export(cr, uid, external_session, context=context)

    while ids:
        ids_to_process = ids[0:step]
        ids = ids[step:]
        external_session.logger.info("Start to read the ressource %s : %s"%(self._name, ids_to_process))
        resources = self._get_oe_resources(cr, uid, external_session, ids_to_process, langs=langs,
                                    smart_export=smart_export, last_exported_date=last_exported_date,
                                    mapping=mapping, mapping_id=mapping_id, context=context)
        if method == 'onebyone':
            for resource_id in ids_to_process:
                external_session.logger.info("Start to transform and send the ressource %s : %s"%(self._name, resource_id))
                self._transform_and_send_one_resource(cr, uid, external_session, resources[resource_id], resource_id, ids_2_date.get(resource_id), mapping, mapping_id, defaults=defaults, context=context)
        else:
            raise osv.except_osv(_('Developper Error'), _('only method export onebyone is implemented in base_external_referentials'))
    #now = datetime.now().strftime(DEFAULT_SERVER_DATETIME_FORMAT)
    #self._set_last_exported_date(cr, uid, external_session, now, context=context)
    return True

@extend(osv.osv)
def _transform_and_send_one_resource(self, cr, uid, external_session, resource, resource_id,
                            update_date, mapping, mapping_id, defaults=None, context=None):
    for key_lang in resource:
        resource[key_lang] = self._transform_one_resource(cr, uid, external_session, 'from_openerp_to_external',
                                            resource[key_lang], mapping=mapping, mapping_id=mapping_id,
                                            defaults=defaults, context=context)
    return self.send_to_external(cr, uid, external_session, {resource_id : resource}, mapping, mapping_id, update_date, context=context)

@extend(osv.osv)
def _export_one_resource(self, cr, uid, external_session, resource_id, context=None):
    defaults = self._get_default_export_values(cr, uid, external_session, context=context)
    mapping, mapping_id = self._init_mapping(cr, uid, external_session.referential_id.id, convertion_type='from_openerp_to_external', context=context)
    langs = self.get_lang_to_export(cr, uid, external_session, context=context)
    resource = self._get_oe_resources(cr, uid, external_session, [resource_id], langs=langs,
                                smart_export=False, last_exported_date=False,
                                mapping=mapping, mapping_id=mapping_id, context=context)[resource_id]
    return self._transform_and_send_one_resource(cr, uid, external_session, resource, resource_id,
                            False, mapping, mapping_id, defaults=defaults, context=context)

@extend(osv.osv)
def multi_lang_read(self, cr, uid, ids, fields_to_read, langs, resources=None, use_multi_lang = True, context=None):
    def is_translatable(field):
        if self._columns.get(field):
            return self._columns[field].translate
        else:
            return self._inherit_fields[field][2].translate

    if not resources:
        resources = {}
    first = True
    for lang in langs:
        ctx = context.copy()
        ctx['lang'] = lang
        for resource in self.read(cr, uid, ids, fields_to_read, context=ctx):
            if not resources.get(resource['id']): resources[resource['id']] = {}
            resources[resource['id']][lang] = resource
        # Give the possibility to not use the field restriction to read
        # Indeed for some e-commerce like magento the api is ugly and some not translatable field
        # are required at each export :S
        if use_multi_lang and first:
            fields_to_read = [field for field in fields_to_read if is_translatable(field)]
            first=False
    return resources

@extend(osv.osv)
def full_read(self, cr, uid, ids, langs, resources, mapping=None, mapping_id=None, context=None):
    fields_to_read = self.get_field_to_export(cr, uid, ids, mapping, mapping_id, context=context)
    return self.multi_lang_read(cr, uid, ids, fields_to_read, langs, resources=resources, context=context)

@extend(osv.osv)
def smart_read(self, cr, uid, ids, langs, resources, group_ids, inherits_group_ids, last_exported_date=None,
                                                                        mapping=None, mapping_id=None, context=None):
    if last_exported_date:
        search_filter = []
        if group_ids:
            if inherits_group_ids:
                search_filter = ['|', ['x_last_update', '>=', last_exported_date], ['%s.x_last_update'%self._inherits[self._inherits.keys()[0]], '>=', last_exported_date]]
        if inherits_group_ids:
            search_filter = [['%s.x_last_update'%self._inherits[self._inherits.keys()[0]], '>=', last_exported_date]]
        resource_ids_full_read = self.search(cr, uid, search_filter, context=context)
        resource_ids_partial_read = [id for id in ids if not id in resource_ids_full_read]
    else:
        resource_ids_full_read = ids
        resource_ids_partial_read = []

    resources = self.full_read(cr, uid, resource_ids_full_read, langs, resources, context=context)

    if resource_ids_partial_read:
        for group in self.pool.get('group.fields').browse(cr, uid, group_ids, context=context):
            resource_ids = self.search(cr, uid, [[group.column_name, '>=', last_exported_date],['id', 'in', resource_ids_partial_read]], context=context)
            fields_to_read = [field.name for field in group.field_ids]
            resources = self.multi_lang_read(cr, uid, resource_ids, fields_to_read, langs, resources=resources, context=context)
    return resources

@extend(osv.osv)
def get_field_to_export(self, cr, uid, ids, mapping, mapping_id, context=None):
    return list(set(self._columns.keys() + self._inherit_fields.keys()))

@extend(osv.osv)
def _get_oe_resources(self, cr, uid, external_session, ids, langs, smart_export=None,
                                            last_exported_date=None, mapping=None, mapping_id=None, context=None):
    resources = None
    if smart_export:
        resources = self.smart_read(cr, uid, ids, langs, resources, smart_export['group_ids'], smart_export['inherits_group_ids'],
                            last_exported_date=last_exported_date, mapping=mapping, mapping_id=mapping_id, context=context)
    else:
        resources = self.full_read(cr, uid, ids, langs, resources, mapping=mapping, mapping_id=mapping_id, context=context)
    return resources


@extend(osv.osv)
def _get_oeid_from_extid_or_alternative_keys(self, cr, uid, vals, external_id, referential_id, alternative_keys, context=None):
    """
    Used in ext_import in order to search the OpenERP resource to update when importing an external resource.
    It searches the reference in ir.model.data and returns the id in ir.model.data and the id of the
    current's model resource, if it really exists (it may not exists, see below)

    As OpenERP cleans up ir_model_data which res_id records have been deleted only at server update
    because that would be a perf penalty, so we take care of it here.

    This method can also be used by inheriting, in order to find and bind resources by another way than ir.model.data when
    the resource is not already imported.
    As instance, search and bind partners by their mails. In such case, it must returns False for the ir_model_data.id and
    the partner to bind for the resource id

    @param vals: vals to create in OpenERP, already evaluated by _transform_one_external_resource
    @param external_id: external id of the resource to create
    @param referential_id: external referential id from where we import the resource
    @return: tuple of (ir.model.data id / False: external id to create in ir.model.data, model resource id / False: resource to create)
    """
    existing_ir_model_data_id = expected_res_id = False
    if not (external_id is None or external_id is False):
        existing_ir_model_data_id, expected_res_id = self._get_expected_oeid\
        (cr, uid, external_id, referential_id, context=context)

    if not expected_res_id and alternative_keys:
        domain = []
        if 'active' in self._columns.keys():
            domain = ['|', ('active', '=', False), ('active', '=', True)]
        for alternative_key in alternative_keys:
            if vals.get(alternative_key):
                domain.append((alternative_key, '=', vals[alternative_key]))
        if domain:
            expected_res_id = self.search(cr, uid, domain, context=context)
            expected_res_id = expected_res_id and expected_res_id[0] or False
    return existing_ir_model_data_id, expected_res_id

@extend(osv.osv)
def _prepare_external_id_vals(self, cr, uid, res_id, ext_id, referential_id, context=None):
    """ Create an external reference for a resource id in the ir.model.data table"""
    ir_model_data_vals = {
                            'name': self.prefixed_id(ext_id),
                            'model': self._name,
                            'res_id': res_id,
                            'referential_id': referential_id,
                            'module': 'extref/' + self.pool.get('external.referential').\
                            read(cr, uid, referential_id, ['name'])['name']
                          }
    return ir_model_data_vals

@extend(osv.osv)
def create_external_id_vals(self, cr, uid, existing_rec_id, external_id, referential_id, context=None):
    """Add the external id in the table ir_model_data"""
    ir_model_data_vals = \
    self._prepare_external_id_vals(cr, uid, existing_rec_id,
                                   external_id, referential_id,
                                   context=context)
    return self.pool.get('ir.model.data').create(cr, uid, ir_model_data_vals, context=context)

########################################################################################################################
#
#                                             END OF EXPORT FEATURES
#
########################################################################################################################





########################################################################################################################
#
#                                             GENERIC TRANSFORM FEATURES
#
########################################################################################################################

@extend(osv.osv)
def _transform_resources(self, cr, uid, external_session, convertion_type, resources, mapping=None, mapping_id=None,
                    mapping_line_filter_ids=None, parent_data=None, defaults=None, context=None):
    """
    Used in ext_import in order to convert all of the external data into OpenERP data

    @param external_data: list of external_data to convert into OpenERP data
    @param referential_id: external referential id from where we import the resource
    @param parent_data: data of the parent, only use when a mapping line have the type 'sub mapping'
    @param defaults: defaults value for data converted
    @return: list of the line converted into OpenERP value
    """
    result= []
    if resources:
        mapping, mapping_id = self._init_mapping(cr, uid, external_session.referential_id.id, convertion_type=convertion_type, mapping_line_filter_ids=mapping_line_filter_ids, mapping=mapping, mapping_id=mapping_id, context=context)
        if mapping[mapping_id].get("mapping_lines"):
            for resource in resources:
                result.append(self._transform_one_resource(cr, uid, external_session, convertion_type, resource,
                                                            mapping, mapping_id, mapping_line_filter_ids, parent_data=parent_data,
                                                            previous_result=result, defaults=defaults, context=context))
    return result

@extend(osv.osv)
def _transform_one_resource(self, cr, uid, external_session, convertion_type, resource, mapping=None, mapping_id=None,
                    mapping_line_filter_ids=None, parent_data=None, previous_result=None, defaults=None, context=None):
    """
    Used in _transform_external_resources in order to convert external row of data into OpenERP data

    @param referential_id: external referential id from where we import the resource
    @param resource: a dictionnary of data, an lxml.objectify object...
    @param mapping dict: dictionnary of mapping {'product.product' : {'mapping_lines' : [...], 'key_for_external_id':'...'}}
    @param previous_result: list of the previous line converted. This is not used here but it's necessary for playing on change on sale order line
    @param defaults: defaults value for the data imported
    @return: dictionary of converted data in OpenERP format
    """

    #Encapsulation of the resource if it not a dictionnary
    #So we can use the same method to read it
    if not isinstance(resource, dict):
        resource = Resource(resource)

    if context is None:
        context = {}
    if defaults is None:
        defaults = {}

    referential_id = external_session.referential_id.id
    mapping, mapping_id = self._init_mapping(cr, uid, referential_id, convertion_type=convertion_type, mapping_line_filter_ids=mapping_line_filter_ids, mapping=mapping, mapping_id=mapping_id, context=context)

    mapping_lines = mapping[mapping_id].get("mapping_lines")
    key_for_external_id = mapping[mapping_id].get("key_for_external_id")

    vals = {} #Dictionary for create record
    sub_mapping_list=[]
    for mapping_line in mapping_lines:
        if convertion_type == 'from_external_to_openerp':
            from_field = mapping_line['external_field']
            if not from_field and mapping_line['evaluation_type'] != 'function':
                from_field = "%s_%s" %(mapping_line['child_mapping_id'][1], mapping_line['child_mapping_id'][0])
            to_field = mapping_line['internal_field']
        elif convertion_type == 'from_openerp_to_external':
            from_field = mapping_line['internal_field']
            to_field = mapping_line['external_field']

        if mapping_line['evaluation_type'] == 'function' or from_field in resource.keys(): #function field should be always played as they can depend on every field
            field_value = resource.get(from_field)
            if mapping_line['evaluation_type'] == 'sub-mapping':
                sub_mapping_list.append(mapping_line)
            else:
                if mapping_line['evaluation_type'] == 'direct':
                    vals[to_field] = self._transform_field(cr, uid, external_session, convertion_type, field_value, mapping_line, context=context)
                else:
                    #Build the space for expr
                    #Seb : removing ifield can be great ?
                    space = {'self': self,
                             'cr': cr,
                             'uid': uid,
                             'external_session': external_session,
                             'resource': resource,
                             'data': resource, #only for compatibility with the old version => deprecated
                             'parent_resource': parent_data, #TODO rename parent_data to parent_resource
                             'referential_id': external_session.referential_id.id,
                             'defaults': defaults,
                             'context': context,
                             'ifield': self._transform_field(cr, uid, external_session, convertion_type, field_value, mapping_line, context=context),
                             'conn': context.get('conn_obj', False),
                             'base64': base64,
                             'vals': vals,
                             'previous_result': previous_result,
                        }
                    #The expression should return value in list of tuple format
                    #eg[('name','Sharoon'),('age',20)] -> vals = {'name':'Sharoon', 'age':20}
                    if convertion_type == 'from_external_to_openerp':
                        mapping_function_key = 'in_function'
                    else:
                        mapping_function_key = 'out_function'
                    try:
                        exec mapping_line[mapping_function_key] in space
                    except Exception, e:
                        #del(space['__builtins__'])
                        raise MappingError(e, mapping_line['name'], self._name)

                    result = space.get('result', False)
                    # Check if result returned by the mapping function is correct : [('field1', value), ('field2', value))]
                    # And fill the vals dict with the results
                    if result:
                        if isinstance(result, list):
                            for each_tuple in result:
                                if isinstance(each_tuple, tuple) and len(each_tuple) == 2:
                                    vals[each_tuple[0]] = each_tuple[1]
                        else:
                            raise MappingError(_('Invalid format for the variable result.'), mapping_line['external_field'], self._name)
    ext_id = False
    if convertion_type == 'from_external_to_openerp' and key_for_external_id and resource.get(key_for_external_id):
        ext_id = resource[key_for_external_id]
        if isinstance(ext_id, str):
            ext_id = ext_id.isdigit() and int(ext_id) or ext_id
        vals.update({'external_id': ext_id})
    if self._name in context.get('do_not_update', []):
        # if the update of the object is not wanted, we skipped the sub_mapping update also. In the function _transform_one_resource, the creation will also be skipped.
        alternative_keys = mapping[mapping_id]['alternative_keys']
        existing_ir_model_data_id, existing_rec_id = self._get_oeid_from_extid_or_alternative_keys\
            (cr, uid, vals, ext_id, referential_id, alternative_keys, context=context)
        if existing_rec_id:
            return {}
    vals = self._merge_with_default_values(cr, uid, external_session, resource, vals, sub_mapping_list, defaults=defaults, context=context)
    vals = self._transform_sub_mapping(cr, uid, external_session, convertion_type, resource, vals, sub_mapping_list, mapping, mapping_id, mapping_line_filter_ids=mapping_line_filter_ids, defaults=defaults, context=context)

    return vals

@extend(osv.osv)
def _transform_field(self, cr, uid, external_session, convertion_type, field_value, mapping_line, context=None):
    field = False
    external_type = mapping_line['external_type']
    internal_type = mapping_line['internal_type']
    internal_field = mapping_line['internal_field']
    if not (field_value is False or field_value is None):
        if internal_type == 'many2one' and mapping_line['evaluation_type']=='direct':
            if external_type not in ['int', 'unicode']:
                raise osv.except_osv(_('User Error'), _('Wrong external type for mapping %s. One2Many object must have for external type string or integer')%(mapping_line['name'],))
            if self._columns.get(internal_field):
                related_obj_name = self._columns[internal_field]._obj
            else:
                related_obj_name = self._inherit_fields[internal_field][2]._obj
            related_obj = self.pool.get(related_obj_name)
            if convertion_type == 'from_external_to_openerp':
                if external_type == 'unicode':
                    #TODO it can be great if we can search on other field
                    related_obj.search(cr, uid, [(related_obj._rec_name, '=', field_value)], context=context)
                else:
                    return related_obj.get_or_create_oeid(cr, uid, external_session, field_value, context=context)
            else:
                if external_type == 'unicode':
                    #TODO it can be great if we can return on other field and not only the name
                    return field_value[1]
                else:
                    return related_obj.get_or_create_extid(cr, uid,external_session, field_value[0], context=context)

        elif external_type == "datetime":
            if not field_value:
                field_value = False
            else:
                datetime_format = mapping_line['datetime_format']
                if convertion_type == 'from_external_to_openerp':
                    datetime_value = datetime.strptime(field_value, datetime_format)
                    if internal_type == 'date':
                        return datetime_value.strftime(DEFAULT_SERVER_DATE_FORMAT)
                    elif internal_type == 'datetime':
                        return datetime_value.strftime(DEFAULT_SERVER_DATETIME_FORMAT)
                else:
                    if internal_type == 'date':
                        datetime_value = datetime.strptime(field_value, DEFAULT_SERVER_DATE_FORMAT)
                    elif internal_type == 'datetime':
                        datetime_value = datetime.strptime(field_value, DEFAULT_SERVER_DATETIME_FORMAT)
                    return datetime_value.strftime(datetime_format)

        elif external_type == 'list' and isinstance(field_value, (str, unicode)):
            # external data sometimes returns ',1,2,3' for a list...
            if field_value:
                casted_field = eval(field_value.strip(','))
            else:
                casted_field= []
            # For a list, external data may returns something like '1,2,3' but also '1' if only
            # one item has been selected. So if the casted field is not iterable, we put it in a tuple: (1,)
            if not hasattr(casted_field, '__iter__'):
                casted_field = (casted_field,)
            field = list(casted_field)
        else:
            if external_type == 'float' and isinstance(field_value, (str, unicode)):
                field_value = field_value.replace(',','.')
                if not field_value:
                    field_value = 0
            field = eval(external_type)(field_value)
    if field in ['None', 'False']:
        field = False

    #Set correct empty value for each type
    if field is False or field is None:
        empty_value = {
            'integer': 0,
            'unicode': '',
            'char': '',
            'date': False,
            'int': 0,
            'float': 0,
            'list': [],
            'dict': {},
            'boolean': False,
            'many2one': False,
            'one2many': [],
            'many2many': [],
            # external types
            'text': '',
            'textarea': '',
            'selection': 0,
            'multiselect': [],
        }
        if convertion_type == 'from_external_to_openerp':
            empty_value['datetime'] = False
        else:
            empty_value['datetime'] = ''
        if internal_type and convertion_type == 'from_external_to_openerp':
            field = empty_value[internal_type]
        elif external_type:
            # if the type is not specified in empty_value,
            # then we consider it will be False, if it
            # should not for an external_type, please add it
            # in empty_value
            field = empty_value.get(external_type, False)

    return field

@extend(osv.osv)
def _merge_with_default_values(self, cr, uid, external_session, ressource, vals, sub_mapping_list, defaults=None, context=None):
    """
    Used in _transform_one_external_resource in order to merge the defaults values, some params are useless here but need in base_sale_multichannels to play the on_change

    @param sub_mapping_list: list of sub-mapping to apply
    @param external_data: list of data to convert into OpenERP data
    @param referential_id: external referential id from where we import the resource
    @param vals: dictionnary of value previously converted
    @param defauls: defaults value for the data imported
    @return: dictionary of converted data in OpenERP format
    """
    for key in defaults:
        if not key in vals:
            vals[key] = defaults[key]
    return vals

@extend(osv.osv)
def _transform_sub_mapping(self, cr, uid, external_session, convertion_type, resource, vals, sub_mapping_list, mapping, mapping_id, mapping_line_filter_ids=None, defaults=None, context=None):
    """
    Used in _transform_one_external_resource in order to call the sub mapping

    @param sub_mapping_list: list of sub-mapping to apply
    @param resource: resource encapsulated in the object Resource or a dictionnary
    @param referential_id: external referential id from where we import the resource
    @param vals: dictionnary of value previously converted
    @param defauls: defaults value for the data imported
    @return: dictionary of converted data in OpenERP format
    """
    if not defaults:
        defaults={}
    ir_model_field_obj = self.pool.get('ir.model.fields')
    for sub_mapping in sub_mapping_list:
        sub_object_name = sub_mapping['child_mapping_id'][1]
        sub_mapping_id = sub_mapping['child_mapping_id'][0]
        if convertion_type == 'from_external_to_openerp':
            from_field = sub_mapping['external_field']
            if not from_field:
                from_field = "%s_%s" %(sub_object_name, sub_mapping_id)
            to_field = sub_mapping['internal_field']

        elif convertion_type == 'from_openerp_to_external':
            from_field = sub_mapping['internal_field']
            to_field = sub_mapping['external_field'] or 'hidden_field_to_split_%s'%from_field # if the field doesn't have any name we assume at that we will split it

        field_value = resource[from_field]
        sub_mapping_obj = self.pool.get(sub_object_name)
        sub_mapping_defaults = sub_mapping_obj._get_default_import_values(cr, uid, external_session, sub_mapping_id, defaults.get(to_field), context=context)

        if field_value:
            transform_args = [cr, uid, external_session, convertion_type, field_value]
            transform_kwargs = {
                'defaults': sub_mapping_defaults,
                'mapping': mapping,
                'mapping_id': sub_mapping_id,
                'mapping_line_filter_ids': mapping_line_filter_ids,
                'parent_data': vals,
                'context': context,
            }

            if sub_mapping['internal_type'] in ['one2many', 'many2many']:
                if not isinstance(field_value, list):
                    transform_args[4] = [field_value]
                if not to_field in vals:
                    vals[to_field] = []
                if convertion_type == 'from_external_to_openerp':
                    lines = sub_mapping_obj._transform_resources(*transform_args, **transform_kwargs)
                else:
                    mapping, sub_mapping_id = self._init_mapping(cr, uid, external_session.referential_id.id, \
                                                                    convertion_type=convertion_type,
                                                                    mapping=mapping,
                                                                    mapping_id=sub_mapping_id,
                                                                    context=context)
                    field_to_read = [x['internal_field'] for x in mapping[sub_mapping_id]['mapping_lines']]
                    sub_resources = sub_mapping_obj.read(cr, uid, field_value, field_to_read, context=context)
                    transform_args[4] = sub_resources
                    lines = sub_mapping_obj._transform_resources(*transform_args, **transform_kwargs)
                for line in lines:
                    if 'external_id' in line:
                        del line['external_id']
                    if convertion_type == 'from_external_to_openerp':
                        if sub_mapping['internal_type'] == 'one2many':
                            #TODO refactor to search the id and alternative keys before the update
                            external_id = vals.get('external_id')
                            alternative_keys = mapping[mapping_id]['alternative_keys']
                            #search id of the parent
                            existing_ir_model_data_id, existing_rec_id = \
                                         self._get_oeid_from_extid_or_alternative_keys(
                                                                cr, uid, vals, external_id,
                                                                external_session.referential_id.id,
                                                                alternative_keys, context=context)
                            vals_to_append = (0, 0, line)
                            if existing_rec_id:
                                sub_external_id = line.get('external_id')
                                if mapping[sub_mapping_id].get('alternative_keys'):
                                    sub_alternative_keys = list(mapping[sub_mapping_id]['alternative_keys'])
                                    if self._columns.get(to_field):
                                        related_field = self._columns[to_field]._fields_id
                                    elif self._inherit_fields.get(to_field):
                                        related_field = self._inherit_fields[to_field][2]._fields_id
                                    sub_alternative_keys.append(related_field)
                                    line[related_field] = existing_rec_id
                                    #search id of the sub_mapping related to the id of the parent
                                    sub_existing_ir_model_data_id, sub_existing_rec_id = \
                                                sub_mapping_obj._get_oeid_from_extid_or_alternative_keys(
                                                                    cr, uid, line, sub_external_id,
                                                                    external_session.referential_id.id,
                                                                    sub_alternative_keys, context=context)
                                    del line[related_field]
                                    if sub_existing_rec_id:
                                        vals_to_append = (1, sub_existing_rec_id, line)
                        vals[to_field].append(vals_to_append)
                    else:
                        vals[to_field].append(line)

            elif sub_mapping['internal_type'] == 'many2one':
                if convertion_type == 'from_external_to_openerp':
                    res = sub_mapping_obj._record_one_external_resource(cr, uid, external_session, field_value,
                                defaults=sub_mapping_defaults, mapping=mapping, mapping_id=sub_mapping_id, context=context)
                    vals[to_field] = res.get('write_id') or res.get('create_id')
                else:
                    sub_resource = sub_mapping_obj.read(cr, uid, field_value[0], context=context)
                    transform_args[4] = sub_resource
                    vals[to_field] = sub_mapping_obj._transform_one_resource(*transform_args, **transform_kwargs)
            else:
                raise osv.except_osv(_('User Error'), _('Error with mapping : %s. Sub mapping can be only apply on one2many, many2one or many2many fields')%(sub_mapping['name'],))
    return vals


########################################################################################################################
#
#                                           END GENERIC TRANSFORM FEATURES
#
########################################################################################################################




