# -*- coding: utf-8 -*-
# Copyright CERN since 2013
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

"""
Judge-Repairer is a daemon to repair stuck replication rules.
"""

import logging
import os
import socket
import threading
import time
import traceback
from copy import deepcopy
from datetime import datetime, timedelta
from random import randint
from re import match

from sqlalchemy.exc import DatabaseError

import rucio.db.sqla.util
from rucio.common import exception
from rucio.common.exception import DatabaseException
from rucio.common.logging import setup_logging, formatted_logger
from rucio.common.utils import daemon_sleep
from rucio.core.heartbeat import live, die, sanity_check
from rucio.core.monitor import record_counter
from rucio.core.rule import repair_rule, get_stuck_rules

graceful_stop = threading.Event()


def rule_repairer(once=False, sleep_time=60):
    """
    Main loop to check for STUCK replication rules
    """

    hostname = socket.gethostname()
    pid = os.getpid()
    current_thread = threading.current_thread()

    paused_rules = {}  # {rule_id: datetime}

    # Make an initial heartbeat so that all judge-repairers have the correct worker number on the next try
    executable = 'judge-repairer'
    heartbeat = live(executable=executable, hostname=hostname, pid=pid, thread=current_thread, older_than=60 * 30)
    prepend_str = 'rule_repairer [%i/%i] : ' % (heartbeat['assign_thread'], heartbeat['nr_threads'])
    logger = formatted_logger(logging.log, prepend_str + '%s')
    graceful_stop.wait(1)

    while not graceful_stop.is_set():
        try:
            # heartbeat
            heartbeat = live(executable=executable, hostname=hostname, pid=pid, thread=current_thread, older_than=60 * 30)
            prepend_str = 'rule_repairer [%i/%i] : ' % (heartbeat['assign_thread'], heartbeat['nr_threads'])
            logger = formatted_logger(logging.log, prepend_str + '%s')

            start = time.time()

            # Refresh paused rules
            iter_paused_rules = deepcopy(paused_rules)
            for key in iter_paused_rules:
                if datetime.utcnow() > paused_rules[key]:
                    del paused_rules[key]

            # Select a bunch of rules for this worker to repair
            rules = get_stuck_rules(total_workers=heartbeat['nr_threads'],
                                    worker_number=heartbeat['assign_thread'],
                                    delta=-1 if once else 1800,
                                    limit=100,
                                    blocked_rules=[key for key in paused_rules])

            logger(logging.DEBUG, 'index query time %f fetch size is %d' % (time.time() - start, len(rules)))

            if not rules and not once:
                logger(logging.DEBUG, 'did not get any work (paused_rules=%s)' % (str(len(paused_rules))))
                daemon_sleep(start, sleep_time, graceful_stop)
            else:
                for rule_id in rules:
                    rule_id = rule_id[0]
                    logger(logging.INFO, 'Repairing rule %s' % (rule_id))
                    if graceful_stop.is_set():
                        break
                    try:
                        start = time.time()
                        repair_rule(rule_id=rule_id)
                        logger(logging.DEBUG, 'repairing of %s took %f' % (rule_id, time.time() - start))
                    except (DatabaseException, DatabaseError) as e:
                        if match('.*ORA-00054.*', str(e.args[0])):
                            paused_rules[rule_id] = datetime.utcnow() + timedelta(seconds=randint(600, 2400))
                            logger(logging.WARNING, 'Locks detected for %s' % (rule_id))
                            record_counter('rule.judge.exceptions.{exception}', labels={'exception': 'LocksDetected'})
                        elif match('.*QueuePool.*', str(e.args[0])):
                            logger(logging.WARNING, traceback.format_exc())
                            record_counter('rule.judge.exceptions.{exception}', labels={'exception': e.__class__.__name__})
                        elif match('.*ORA-03135.*', str(e.args[0])):
                            logger(logging.WARNING, traceback.format_exc())
                            record_counter('rule.judge.exceptions.{exception}', labels={'exception': e.__class__.__name__})
                        else:
                            logger(logging.ERROR, traceback.format_exc())
                            record_counter('rule.judge.exceptions.{exception}', labels={'exception': e.__class__.__name__})

        except (DatabaseException, DatabaseError) as e:
            if match('.*QueuePool.*', str(e.args[0])):
                logger(logging.WARNING, traceback.format_exc())
                record_counter('rule.judge.exceptions.{exception}', labels={'exception': e.__class__.__name__})
            elif match('.*ORA-03135.*', str(e.args[0])):
                logger(logging.WARNING, traceback.format_exc())
                record_counter('rule.judge.exceptions.{exception}', labels={'exception': e.__class__.__name__})
            else:
                logger(logging.CRITICAL, traceback.format_exc())
                record_counter('rule.judge.exceptions.{exception}', labels={'exception': e.__class__.__name__})
        except Exception as e:
            logging.critical(traceback.format_exc())
            record_counter('rule.judge.exceptions.{exception}', labels={'exception': e.__class__.__name__})
        if once:
            break

    die(executable=executable, hostname=hostname, pid=pid, thread=current_thread)


def stop(signum=None, frame=None):
    """
    Graceful exit.
    """

    graceful_stop.set()


def run(once=False, threads=1, sleep_time=60):
    """
    Starts up the Judge-Repairer threads.
    """
    setup_logging()

    if rucio.db.sqla.util.is_old_db():
        raise exception.DatabaseException('Database was not updated, daemon won\'t start')

    executable = 'judge-repairer'
    hostname = socket.gethostname()
    sanity_check(executable=executable, hostname=hostname)

    if once:
        rule_repairer(once)
    else:
        logging.info('Repairer starting %s threads' % str(threads))
        threads = [threading.Thread(target=rule_repairer, kwargs={'once': once,
                                                                  'sleep_time': sleep_time}) for i in range(0, threads)]
        [t.start() for t in threads]
        # Interruptible joins require a timeout.
        while threads[0].is_alive():
            [t.join(timeout=3.14) for t in threads]
