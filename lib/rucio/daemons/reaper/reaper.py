# -*- coding: utf-8 -*-
# Copyright CERN since 2019
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

'''
Reaper is a daemon to manage file deletion.
'''

from __future__ import division

import logging
import os
import random
import socket
import threading
import time
import traceback
from collections import OrderedDict
from configparser import NoOptionError, NoSectionError
from datetime import datetime, timedelta
from math import ceil
from typing import TYPE_CHECKING

from dogpile.cache.api import NO_VALUE
from prometheus_client import Gauge
from sqlalchemy.exc import DatabaseError, IntegrityError

import rucio.db.sqla.util
from rucio.common.config import config_get, config_get_bool
from rucio.common.cache import make_region_memcached
from rucio.common.exception import (DatabaseException, RSENotFound,
                                    ReplicaUnAvailable, ReplicaNotFound, ServiceUnavailable,
                                    RSEAccessDenied, ResourceTemporaryUnavailable, SourceNotFound,
                                    VONotFound)
from rucio.common.logging import formatted_logger, setup_logging
from rucio.common.utils import chunks, daemon_sleep
from rucio.core import monitor
from rucio.core.credential import get_signed_url
from rucio.core.heartbeat import live, die, sanity_check, list_payload_counts
from rucio.core.message import add_message
from rucio.core.replica import list_and_mark_unlocked_replicas, list_and_mark_unlocked_replicas_no_temp_table, delete_replicas
from rucio.core.rse import list_rses, get_rse_limits, get_rse_usage, list_rse_attributes, get_rse_protocols
from rucio.core.rse_expression_parser import parse_expression
from rucio.core.rule import get_evaluation_backlog
from rucio.core.vo import list_vos
from rucio.rse import rsemanager as rsemgr

if TYPE_CHECKING:
    from typing import Callable, Tuple

GRACEFUL_STOP = threading.Event()

REGION = make_region_memcached(expiration_time=600)

DELETION_COUNTER = monitor.MultiCounter(prom='rucio_daemons_reaper_deletion_done', statsd='reaper.deletion.done',
                                        documentation='Number of deleted replicas')
EXCLUDED_RSE_GAUGE = Gauge('rucio_daemons_reaper_excluded_rses', 'Temporarly excluded RSEs', labelnames=('rse',))


def get_rses_to_process(rses, include_rses, exclude_rses, vos):
    """
    Return the list of RSEs to process based on rses, include_rses and exclude_rses

    :param rses:               List of RSEs the reaper should work against. If empty, it considers all RSEs.
    :param exclude_rses:       RSE expression to exclude RSEs from the Reaper.
    :param include_rses:       RSE expression to include RSEs.
    :param vos:                VOs on which to look for RSEs. Only used in multi-VO mode.
                               If None, we either use all VOs if run from "def"

    :returns: A list of RSEs to process
    """
    multi_vo = config_get_bool('common', 'multi_vo', raise_exception=False, default=False)
    if not multi_vo:
        if vos:
            logging.log(logging.WARNING, 'Ignoring argument vos, this is only applicable in a multi-VO setup.')
        vos = ['def']
    else:
        if vos:
            invalid = set(vos) - set([v['vo'] for v in list_vos()])
            if invalid:
                msg = 'VO{} {} cannot be found'.format('s' if len(invalid) > 1 else '', ', '.join([repr(v) for v in invalid]))
                raise VONotFound(msg)
        else:
            vos = [v['vo'] for v in list_vos()]
        logging.log(logging.INFO, 'Reaper: This instance will work on VO%s: %s' % ('s' if len(vos) > 1 else '', ', '.join([v for v in vos])))

    pid = os.getpid()
    cache_key = 'rses_to_process_%s' % pid
    if multi_vo:
        cache_key += '@%s' % '-'.join(vo for vo in vos)

    result = REGION.get(cache_key)
    if result is not NO_VALUE:
        return result

    all_rses = []
    for vo in vos:
        all_rses.extend(list_rses(filters={'vo': vo}))

    if rses:
        invalid = set(rses) - set([rse['rse'] for rse in all_rses])
        if invalid:
            msg = 'RSE{} {} cannot be found'.format('s' if len(invalid) > 1 else '',
                                                    ', '.join([repr(rse) for rse in invalid]))
            raise RSENotFound(msg)
        rses = [rse for rse in all_rses if rse['rse'] in rses]
    else:
        rses = all_rses

    if include_rses:
        included_rses = parse_expression(include_rses)
        rses = [rse for rse in rses if rse in included_rses]

    if exclude_rses:
        excluded_rses = parse_expression(exclude_rses)
        rses = [rse for rse in rses if rse not in excluded_rses]

    REGION.set(cache_key, rses)
    logging.log(logging.INFO, 'Reaper: This instance will work on RSEs: %s', ', '.join([rse['rse'] for rse in rses]))
    return rses


def delete_from_storage(replicas, prot, rse_info, staging_areas, auto_exclude_threshold, logger=logging.log):
    deleted_files = []
    rse_name = rse_info['rse']
    rse_id = rse_info['id']
    noaccess_attempts = 0
    pfns_to_bulk_delete = []
    try:
        prot.connect()
        for replica in replicas:
            # Physical deletion
            try:
                deletion_dict = {'scope': replica['scope'].external,
                                 'name': replica['name'],
                                 'rse': rse_name,
                                 'file-size': replica['bytes'],
                                 'bytes': replica['bytes'],
                                 'url': replica['pfn'],
                                 'protocol': prot.attributes['scheme']}
                if replica['scope'].vo != 'def':
                    deletion_dict['vo'] = replica['scope'].vo
                logger(logging.DEBUG, 'Deletion ATTEMPT of %s:%s as %s on %s', replica['scope'], replica['name'], replica['pfn'], rse_name)
                start = time.time()
                # For STAGING RSEs, no physical deletion
                if rse_id in staging_areas:
                    logger(logging.WARNING, 'Deletion STAGING of %s:%s as %s on %s, will only delete the catalog and not do physical deletion', replica['scope'], replica['name'], replica['pfn'], rse_name)
                    deleted_files.append({'scope': replica['scope'], 'name': replica['name']})
                    continue

                if replica['pfn']:
                    pfn = replica['pfn']
                    # sign the URL if necessary
                    if prot.attributes['scheme'] == 'https' and rse_info['sign_url'] is not None:
                        pfn = get_signed_url(rse_id, rse_info['sign_url'], 'delete', pfn)
                    if prot.attributes['scheme'] == 'globus':
                        pfns_to_bulk_delete.append(replica['pfn'])
                    else:
                        prot.delete(pfn)
                else:
                    logger(logging.WARNING, 'Deletion UNAVAILABLE of %s:%s as %s on %s', replica['scope'], replica['name'], replica['pfn'], rse_name)

                monitor.record_timer('daemons.reaper.delete.{scheme}.{rse}', (time.time() - start) * 1000, labels={'scheme': prot.attributes['scheme'], 'rse': rse_name})
                duration = time.time() - start

                deleted_files.append({'scope': replica['scope'], 'name': replica['name']})

                deletion_dict['duration'] = duration
                add_message('deletion-done', deletion_dict)
                logger(logging.INFO, 'Deletion SUCCESS of %s:%s as %s on %s in %.2f seconds', replica['scope'], replica['name'], replica['pfn'], rse_name, duration)

            except SourceNotFound:
                duration = time.time() - start
                err_msg = 'Deletion NOTFOUND of %s:%s as %s on %s in %.2f seconds' % (replica['scope'], replica['name'], replica['pfn'], rse_name, duration)
                logger(logging.WARNING, '%s', err_msg)
                deletion_dict['reason'] = 'File Not Found'
                deletion_dict['duration'] = duration
                add_message('deletion-not-found', deletion_dict)
                deleted_files.append({'scope': replica['scope'], 'name': replica['name']})

            except (ServiceUnavailable, RSEAccessDenied, ResourceTemporaryUnavailable) as error:
                duration = time.time() - start
                logger(logging.WARNING, 'Deletion NOACCESS of %s:%s as %s on %s: %s in %.2f', replica['scope'], replica['name'], replica['pfn'], rse_name, str(error), duration)
                deletion_dict['reason'] = str(error)
                deletion_dict['duration'] = duration
                add_message('deletion-failed', deletion_dict)
                noaccess_attempts += 1
                if noaccess_attempts >= auto_exclude_threshold:
                    logger(logging.INFO, 'Too many (%d) NOACCESS attempts for %s. RSE will be temporarly excluded.', noaccess_attempts, rse_name)
                    REGION.set('temporary_exclude_%s' % rse_id, True)
                    labels = {'rse': rse_name}
                    EXCLUDED_RSE_GAUGE.labels(**labels).set(1)
                    break

            except Exception as error:
                duration = time.time() - start
                logger(logging.CRITICAL, 'Deletion CRITICAL of %s:%s as %s on %s in %.2f seconds : %s', replica['scope'], replica['name'], replica['pfn'], rse_name, duration, str(traceback.format_exc()))
                deletion_dict['reason'] = str(error)
                deletion_dict['duration'] = duration
                add_message('deletion-failed', deletion_dict)

        if pfns_to_bulk_delete and prot.attributes['scheme'] == 'globus':
            logger(logging.DEBUG, 'Attempting bulk delete on RSE %s for scheme %s', rse_name, prot.attributes['scheme'])
            prot.bulk_delete(pfns_to_bulk_delete)

    except (ServiceUnavailable, RSEAccessDenied, ResourceTemporaryUnavailable) as error:
        for replica in replicas:
            logger(logging.WARNING, 'Deletion NOACCESS of %s:%s as %s on %s: %s', replica['scope'], replica['name'], replica['pfn'], rse_name, str(error))
            payload = {'scope': replica['scope'].external,
                       'name': replica['name'],
                       'rse': rse_name,
                       'file-size': replica['bytes'],
                       'bytes': replica['bytes'],
                       'url': replica['pfn'],
                       'reason': str(error),
                       'protocol': prot.attributes['scheme']}
            if replica['scope'].vo != 'def':
                payload['vo'] = replica['scope'].vo
            add_message('deletion-failed', payload)
        logger(logging.INFO, 'Cannot connect to %s. RSE will be temporarly excluded.', rse_name)
        REGION.set('temporary_exclude_%s' % rse_id, True)
        labels = {'rse': rse_name}
        EXCLUDED_RSE_GAUGE.labels(**labels).set(1)
    finally:
        prot.close()
    return deleted_files


def get_rses_to_hostname_mapping():
    """
    Return a dictionaries mapping the RSEs to the hostname of the SE

    :returns:      Dictionary with RSE_id as key and (hostname, rse_info) as value
    """

    result = REGION.get('rse_hostname_mapping')
    if result is NO_VALUE:
        result = {}
        all_rses = list_rses()
        for rse in all_rses:
            try:
                rse_protocol = get_rse_protocols(rse_id=rse['id'])
            except RSENotFound:
                logging.log(logging.WARNING, 'RSE deleted while constructing rse-to-hostname mapping. Skipping %s', rse['rse'])
                continue

            for prot in rse_protocol['protocols']:
                if prot['domains']['wan']['delete'] == 1:
                    result[rse['id']] = (prot['hostname'], rse_protocol)
            if rse['id'] not in result:
                logging.log(logging.WARNING, 'No default delete protocol for %s', rse['rse'])

        REGION.set('rse_hostname_mapping', result)
        return result

    return result


def get_max_deletion_threads_by_hostname(hostname):
    """
    Internal method to check RSE usage and limits.

    :param hostname: the hostname of the SE

    :returns: The maximum deletion thread for the SE.
    """
    result = REGION.get('max_deletion_threads_%s' % hostname)
    if result is NO_VALUE:
        try:
            max_deletion_thread = config_get('reaper', 'max_deletion_threads_%s' % hostname)
        except (NoOptionError, NoSectionError, RuntimeError):
            try:
                max_deletion_thread = config_get('reaper', 'nb_workers_by_hostname')
            except (NoOptionError, NoSectionError, RuntimeError):
                max_deletion_thread = 5
        REGION.set('max_deletion_threads_%s' % hostname, max_deletion_thread)
        result = max_deletion_thread
    return result


def __check_rse_usage(rse: str, rse_id: str, greedy: bool = False,
                      logger: 'Callable' = logging.log) -> 'Tuple[int, bool]':
    """
    Internal method to check RSE usage and limits.

    :param rse:     The RSE name.
    :param rse_id:  The RSE id.
    :param greedy:  If True, needed_free_space will be set to 1TB regardless of actual rse usage.

    :returns: needed_free_space, only_delete_obsolete.
    """

    result = REGION.get('rse_usage_%s' % rse_id)
    if result is NO_VALUE:
        needed_free_space, used, free, obsolete = 0, 0, 0, 0

        # First of all check if greedy mode is enabled for this RSE or generally
        attributes = list_rse_attributes(rse_id=rse_id)
        rse_attr_greedy = attributes.get('greedyDeletion', False)
        if greedy or rse_attr_greedy:
            result = (1000000000000, False)
            REGION.set('rse_usage_%s' % rse_id, result)
            return result

        # Get RSE limits
        limits = get_rse_limits(rse_id=rse_id)
        min_free_space = limits.get('MinFreeSpace', 0)

        # Check from which sources to get used and total spaces
        # Default is storage
        source_for_total_space = attributes.get('source_for_total_space', 'storage')
        source_for_used_space = attributes.get('source_for_used_space', 'storage')

        logger(logging.DEBUG, 'RSE: %s, source_for_total_space: %s, source_for_used_space: %s',
               rse, source_for_total_space, source_for_used_space)

        # Get total, used and obsolete space
        rse_usage = get_rse_usage(rse_id=rse_id)
        usage = [entry for entry in rse_usage if entry['source'] == 'obsolete']
        for var in usage:
            obsolete = var['used']
            break
        usage = [entry for entry in rse_usage if entry['source'] == source_for_total_space]

        # If no information is available about disk space, do nothing except if there are replicas with Epoch tombstone
        if not usage:
            if not obsolete:
                result = (needed_free_space, False)
                REGION.set('rse_usage_%s' % rse_id, result)
                return result
            result = (obsolete, True)
            REGION.set('rse_usage_%s' % rse_id, result)
            return result

        # Extract the total and used space
        for var in usage:
            total, used = var['total'], var['used']
            break

        if source_for_total_space != source_for_used_space:
            usage = [entry for entry in rse_usage if entry['source'] == source_for_used_space]
            if not usage:
                result = (needed_free_space, False)
                REGION.set('rse_usage_%s' % rse_id, result)
                return result
            for var in usage:
                used = var['used']
                break

        free = total - used
        if min_free_space:
            needed_free_space = min_free_space - free

        # If needed_free_space negative, nothing to delete except if some Epoch tombstoned replicas
        if needed_free_space <= 0:
            result = (obsolete, True)
        else:
            result = (needed_free_space, False)
        REGION.set('rse_usage_%s' % rse_id, result)
        return result

    return result


def reaper(rses, include_rses, exclude_rses, vos=None, chunk_size=100, once=False, greedy=False,
           scheme=None, delay_seconds=0, sleep_time=60, auto_exclude_threshold=100, auto_exclude_timeout=600):
    """
    Main loop to select and delete files.

    :param rses:                   List of RSEs the reaper should work against. If empty, it considers all RSEs.
    :param include_rses:           RSE expression to include RSEs.
    :param exclude_rses:           RSE expression to exclude RSEs from the Reaper.
    :param vos:                    VOs on which to look for RSEs. Only used in multi-VO mode.
                                   If None, we either use all VOs if run from "def", or the current VO otherwise.
    :param chunk_size:             The size of chunk for deletion.
    :param once:                   If True, only runs one iteration of the main loop.
    :param greedy:                 If True, delete right away replicas with tombstone.
    :param scheme:                 Force the reaper to use a particular protocol, e.g., mock.
    :param delay_seconds:          The delay to query replicas in BEING_DELETED state.
    :param sleep_time:             Time between two cycles.
    :param auto_exclude_threshold: Number of service unavailable exceptions after which the RSE gets temporarily excluded.
    :param auto_exclude_timeout:   Timeout for temporarily excluded RSEs.
    """
    hostname = socket.getfqdn()
    executable = 'reaper'
    pid = os.getpid()
    hb_thread = threading.current_thread()
    sanity_check(executable=executable, hostname=hostname)
    heart_beat = live(executable, hostname, pid, hb_thread)
    prepend_str = 'reaper[%i/%i] ' % (heart_beat['assign_thread'], heart_beat['nr_threads'])
    logger = formatted_logger(logging.log, prepend_str + '%s')

    logger(logging.INFO, 'Reaper starting')

    if not once:
        GRACEFUL_STOP.wait(10)  # To prevent running on the same partition if all the reapers restart at the same time
    heart_beat = live(executable, hostname, pid, hb_thread)
    prepend_str = 'reaper[%i/%i] ' % (heart_beat['assign_thread'], heart_beat['nr_threads'])
    logger = formatted_logger(logging.log, prepend_str + '%s')
    logger(logging.INFO, 'Reaper started')

    while not GRACEFUL_STOP.is_set():
        # try to get auto exclude parameters from the config table. Otherwise use CLI parameters.
        try:
            auto_exclude_threshold = config_get('reaper', 'auto_exclude_threshold', default=auto_exclude_threshold)
            auto_exclude_timeout = config_get('reaper', 'auto_exclude_timeout', default=auto_exclude_timeout)
        except (NoOptionError, NoSectionError, RuntimeError):
            pass

        # Check if there is a Judge Evaluator backlog
        try:
            max_evaluator_backlog_count = config_get('reaper', 'max_evaluator_backlog_count')
        except (NoOptionError, NoSectionError, RuntimeError):
            max_evaluator_backlog_count = None
        try:
            max_evaluator_backlog_duration = config_get('reaper', 'max_evaluator_backlog_duration')
        except (NoOptionError, NoSectionError, RuntimeError):
            max_evaluator_backlog_duration = None
        if max_evaluator_backlog_count or max_evaluator_backlog_duration:
            backlog = get_evaluation_backlog()
            if max_evaluator_backlog_count and \
               backlog[0] and \
               max_evaluator_backlog_duration and \
               backlog[1] and \
               backlog[0] > max_evaluator_backlog_count and \
               backlog[1] < datetime.utcnow() - timedelta(minutes=max_evaluator_backlog_duration):
                logger(logging.ERROR, 'Reaper: Judge evaluator backlog count and duration hit, stopping operation')
                GRACEFUL_STOP.wait(30)
                continue
            elif max_evaluator_backlog_count and backlog[0] and backlog[0] > max_evaluator_backlog_count:
                logger(logging.ERROR, 'Reaper: Judge evaluator backlog count hit, stopping operation')
                GRACEFUL_STOP.wait(30)
                continue
            elif max_evaluator_backlog_duration and backlog[1] and backlog[1] < datetime.utcnow() - timedelta(minutes=max_evaluator_backlog_duration):
                logger(logging.ERROR, 'Reaper: Judge evaluator backlog duration hit, stopping operation')
                GRACEFUL_STOP.wait(30)
                continue

        rses_to_process = get_rses_to_process(rses, include_rses, exclude_rses, vos)
        if not rses_to_process:
            logger(logging.ERROR, 'Reaper: No RSEs found. Will sleep for 30 seconds')
            GRACEFUL_STOP.wait(30)
            continue
        start_time = time.time()
        try:
            staging_areas = []
            dict_rses = {}
            heart_beat = live(executable, hostname, pid, hb_thread, older_than=3600)
            prepend_str = 'reaper[%i/%i] ' % (heart_beat['assign_thread'], heart_beat['nr_threads'])
            logger = formatted_logger(logging.log, prepend_str + '%s')
            tot_needed_free_space = 0
            for rse in rses_to_process:
                # Check if the RSE is a staging area
                if rse['staging_area']:
                    staging_areas.append(rse['rse'])
                # Check if RSE is blocklisted
                if rse['availability'] % 2 == 0:
                    logger(logging.DEBUG, 'RSE %s is blocklisted for delete', rse['rse'])
                    continue
                needed_free_space, only_delete_obsolete = __check_rse_usage(rse['rse'], rse['id'], greedy=greedy, logger=logger)
                if needed_free_space:
                    dict_rses[(rse['rse'], rse['id'])] = [needed_free_space, only_delete_obsolete]
                    tot_needed_free_space += needed_free_space
                elif only_delete_obsolete:
                    dict_rses[(rse['rse'], rse['id'])] = [needed_free_space, only_delete_obsolete]
                else:
                    logger(logging.DEBUG, 'Nothing to delete on %s', rse['rse'])

            # Ordering the RSEs based on the needed free space
            sorted_dict_rses = OrderedDict(sorted(dict_rses.items(), key=lambda x: x[1][0], reverse=True))
            logger(logging.DEBUG, 'List of RSEs to process ordered by needed space desc: %s', str(sorted_dict_rses))

            # Get the mapping between the RSE and the hostname used for deletion. The dictionary has RSE as key and (hostanme, rse_info) as value
            rses_hostname_mapping = get_rses_to_hostname_mapping()
            # logger(logging.DEBUG, '%s Mapping RSEs to hostnames used for deletion : %s', prepend_str, str(rses_hostname_mapping))

            list_rses_mult = []

            # Loop over the RSEs. rse_key = (rse, rse_id) and fill list_rses_mult that contains all RSEs to process with different multiplicity
            for rse_key in dict_rses:
                rse_name, rse_id = rse_key
                # The length of the deletion queue scales inversily with the number of workers
                # The ceil increase the weight of the RSE with small amount of files to delete
                if tot_needed_free_space:
                    max_workers = ceil(dict_rses[rse_key][0] / tot_needed_free_space * 1000 / heart_beat['nr_threads'])
                else:
                    max_workers = 1

                list_rses_mult.extend([(rse_name, rse_id, dict_rses[rse_key][0], dict_rses[rse_key][1]) for _ in range(int(max_workers))])
            random.shuffle(list_rses_mult)

            paused_rses = []
            for rse_name, rse_id, needed_free_space, max_being_deleted_files in list_rses_mult:
                result = REGION.get('pause_deletion_%s' % rse_id, expiration_time=120)
                if result is not NO_VALUE:
                    paused_rses.append(rse_name)
                    logger(logging.DEBUG, 'Not enough replicas to delete on %s during the previous cycle. Deletion paused for a while', rse_name)
                    continue
                result = REGION.get('temporary_exclude_%s' % rse_id, expiration_time=auto_exclude_timeout)
                if result is not NO_VALUE:
                    logger(logging.WARNING, 'Too many failed attempts for %s in last cycle. RSE is temporarly excluded.', rse_name)
                    labels = {'rse': rse_name}
                    EXCLUDED_RSE_GAUGE.labels(**labels).set(1)
                    continue
                labels = {'rse': rse_name}
                EXCLUDED_RSE_GAUGE.labels(**labels).set(0)
                percent = 0
                if tot_needed_free_space:
                    percent = needed_free_space / tot_needed_free_space * 100
                logger(logging.DEBUG, 'Working on %s. Percentage of the total space needed %.2f', rse_name, percent)
                try:
                    rse_hostname, rse_info = rses_hostname_mapping[rse_id]
                except KeyError:
                    logger(logging.DEBUG, "Hostname lookup for %s failed.", rse_name)
                    REGION.set('pause_deletion_%s' % rse_id, True)
                    continue
                rse_hostname_key = '%s,%s' % (rse_id, rse_hostname)
                payload_cnt = list_payload_counts(executable, older_than=600, hash_executable=None, session=None)
                # logger(logging.DEBUG, '%s Payload count : %s', prepend_str, str(payload_cnt))
                tot_threads_for_hostname = 0
                tot_threads_for_rse = 0
                for key in payload_cnt:
                    if key and key.find(',') > -1:
                        if key.split(',')[1] == rse_hostname:
                            tot_threads_for_hostname += payload_cnt[key]
                        if key.split(',')[0] == str(rse_id):
                            tot_threads_for_rse += payload_cnt[key]
                max_deletion_thread = get_max_deletion_threads_by_hostname(rse_hostname)
                if rse_hostname_key in payload_cnt and tot_threads_for_hostname >= max_deletion_thread:
                    logger(logging.DEBUG, 'Too many deletion threads for %s on RSE %s. Back off', rse_hostname, rse_name)
                    # Might need to reschedule a try on this RSE later in the same cycle
                    continue
                logger(logging.INFO, 'Nb workers on %s smaller than the limit (current %i vs max %i). Starting new worker on RSE %s', rse_hostname, tot_threads_for_hostname, max_deletion_thread, rse_name)
                live(executable, hostname, pid, hb_thread, older_than=600, hash_executable=None, payload=rse_hostname_key, session=None)
                logger(logging.DEBUG, 'Total deletion workers for %s : %i', rse_hostname, tot_threads_for_hostname + 1)
                # List and mark BEING_DELETED the files to delete
                del_start_time = time.time()
                only_delete_obsolete = dict_rses[(rse_name, rse_id)][1]
                try:
                    use_temp_tables = config_get_bool('core', 'use_temp_tables', default=False)
                    with monitor.record_timer_block('reaper.list_unlocked_replicas'):
                        if only_delete_obsolete:
                            logger(logging.DEBUG, 'Will run list_and_mark_unlocked_replicas on %s. No space needed, will only delete EPOCH tombstoned replicas', rse_name)
                        if use_temp_tables:
                            replicas = list_and_mark_unlocked_replicas(limit=chunk_size,
                                                                       bytes_=needed_free_space,
                                                                       rse_id=rse_id,
                                                                       delay_seconds=delay_seconds,
                                                                       only_delete_obsolete=only_delete_obsolete,
                                                                       session=None)
                        else:
                            replicas = list_and_mark_unlocked_replicas_no_temp_table(limit=chunk_size,
                                                                                     bytes_=needed_free_space,
                                                                                     rse_id=rse_id,
                                                                                     delay_seconds=delay_seconds,
                                                                                     only_delete_obsolete=only_delete_obsolete,
                                                                                     session=None)
                    logger(logging.DEBUG, 'list_and_mark_unlocked_replicas on %s for %s bytes in %s seconds: %s replicas', rse_name, needed_free_space, time.time() - del_start_time, len(replicas))
                    if len(replicas) < chunk_size:
                        logger(logging.DEBUG, 'Not enough replicas to delete on %s (%s requested vs %s returned). Will skip any new attempts on this RSE until next cycle', rse_name, chunk_size, len(replicas))
                        REGION.set('pause_deletion_%s' % rse_id, True)

                except (DatabaseException, IntegrityError, DatabaseError) as error:
                    logger(logging.ERROR, '%s', str(error))
                    continue
                except Exception:
                    logger(logging.CRITICAL, 'Exception', exc_info=True)
                # Physical  deletion will take place there
                try:
                    prot = rsemgr.create_protocol(rse_info, 'delete', scheme=scheme, logger=logger)
                    for file_replicas in chunks(replicas, chunk_size):
                        # Refresh heartbeat
                        live(executable, hostname, pid, hb_thread, older_than=600, hash_executable=None, payload=rse_hostname_key, session=None)
                        del_start_time = time.time()
                        for replica in file_replicas:
                            try:
                                replica['pfn'] = str(list(rsemgr.lfns2pfns(rse_settings=rse_info,
                                                                           lfns=[{'scope': replica['scope'].external, 'name': replica['name'], 'path': replica['path']}],
                                                                           operation='delete', scheme=scheme).values())[0])
                            except (ReplicaUnAvailable, ReplicaNotFound) as error:
                                logger(logging.WARNING, 'Failed get pfn UNAVAILABLE replica %s:%s on %s with error %s', replica['scope'], replica['name'], rse_name, str(error))
                                replica['pfn'] = None

                            except Exception:
                                logger(logging.CRITICAL, 'Exception', exc_info=True)

                        deleted_files = delete_from_storage(file_replicas, prot, rse_info, staging_areas, auto_exclude_threshold, logger=logger)
                        logger(logging.INFO, '%i files processed in %s seconds', len(file_replicas), time.time() - del_start_time)

                        # Then finally delete the replicas
                        del_start = time.time()
                        with monitor.record_timer_block('reaper.delete_replicas'):
                            delete_replicas(rse_id=rse_id, files=deleted_files)
                        logger(logging.DEBUG, 'delete_replicas successed on %s : %s replicas in %s seconds', rse_name, len(deleted_files), time.time() - del_start)
                        DELETION_COUNTER.inc(len(deleted_files))
                except Exception:
                    logger(logging.CRITICAL, 'Exception', exc_info=True)

            if paused_rses:
                logger(logging.INFO, 'Deletion paused for a while for following RSEs: %s', ', '.join(paused_rses))

            if once:
                break

            daemon_sleep(start_time=start_time, sleep_time=sleep_time, graceful_stop=GRACEFUL_STOP, logger=logger)

        except DatabaseException as error:
            logger(logging.WARNING, 'Reaper:  %s', str(error))
        except Exception:
            logger(logging.CRITICAL, 'Exception', exc_info=True)
        finally:
            if once:
                break

    die(executable=executable, hostname=hostname, pid=pid, thread=hb_thread)
    logger(logging.INFO, 'Graceful stop requested')
    logger(logging.INFO, 'Graceful stop done')
    return


def stop(signum=None, frame=None):
    """
    Graceful exit.
    """
    GRACEFUL_STOP.set()


def run(threads=1, chunk_size=100, once=False, greedy=False, rses=None, scheme=None, exclude_rses=None, include_rses=None, vos=None, delay_seconds=0, sleep_time=60, auto_exclude_threshold=100, auto_exclude_timeout=600):
    """
    Starts up the reaper threads.

    :param threads:                The total number of workers.
    :param chunk_size:             The size of chunk for deletion.
    :param threads_per_worker:     Total number of threads created by each worker.
    :param once:                   If True, only runs one iteration of the main loop.
    :param greedy:                 If True, delete right away replicas with tombstone.
    :param rses:                   List of RSEs the reaper should work against.
                                   If empty, it considers all RSEs.
    :param scheme:                 Force the reaper to use a particular protocol/scheme, e.g., mock.
    :param exclude_rses:           RSE expression to exclude RSEs from the Reaper.
    :param include_rses:           RSE expression to include RSEs.
    :param vos:                    VOs on which to look for RSEs. Only used in multi-VO mode.
                                   If None, we either use all VOs if run from "def",
                                   or the current VO otherwise.
    :param delay_seconds:          The delay to query replicas in BEING_DELETED state.
    :param sleep_time:             Time between two cycles.
    :param auto_exclude_threshold: Number of service unavailable exceptions after which the RSE gets temporarily excluded.
    :param auto_exclude_timeout:   Timeout for temporarily excluded RSEs.
    """
    setup_logging()

    if rucio.db.sqla.util.is_old_db():
        raise DatabaseException('Database was not updated, daemon won\'t start')

    logging.log(logging.INFO, 'main: starting processes')
    rses_to_process = get_rses_to_process(rses, include_rses, exclude_rses, vos)
    if not rses_to_process:
        logging.log(logging.ERROR, 'Reaper: No RSEs found. Exiting.')
        return

    logging.log(logging.INFO, 'Reaper: This instance will work on RSEs: %s', ', '.join([rse['rse'] for rse in rses_to_process]))

    # To populate the cache
    get_rses_to_hostname_mapping()

    logging.log(logging.INFO, 'starting reaper threads')
    threads_list = [threading.Thread(target=reaper, kwargs={'once': once,
                                                            'rses': rses,
                                                            'include_rses': include_rses,
                                                            'exclude_rses': exclude_rses,
                                                            'vos': vos,
                                                            'chunk_size': chunk_size,
                                                            'greedy': greedy,
                                                            'sleep_time': sleep_time,
                                                            'delay_seconds': delay_seconds,
                                                            'scheme': scheme,
                                                            'auto_exclude_threshold': auto_exclude_threshold,
                                                            'auto_exclude_timeout': auto_exclude_timeout}) for _ in range(0, threads)]

    for thread in threads_list:
        thread.start()

    logging.log(logging.INFO, 'waiting for interrupts')

    # Interruptible joins require a timeout.
    while threads_list:
        threads_list = [thread.join(timeout=3.14) for thread in threads_list if thread and thread.is_alive()]
