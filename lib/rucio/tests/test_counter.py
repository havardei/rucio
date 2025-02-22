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

import unittest

import pytest

from rucio.common.config import config_get_bool
from rucio.common.types import InternalAccount
from rucio.core import account_counter, rse_counter
from rucio.core.account import get_usage
from rucio.core.rse import get_rse_id
from rucio.daemons.abacus.account import account_update
from rucio.daemons.abacus.rse import rse_update
from rucio.db.sqla import session, models
from rucio.tests.common_server import get_vo


@pytest.mark.noparallel(reason='uses pre-defined RSE, fails when run in parallel')
class TestCoreRSECounter(unittest.TestCase):
    def setUp(self):
        if config_get_bool('common', 'multi_vo', raise_exception=False, default=False):
            self.vo = {'vo': get_vo()}
        else:
            self.vo = {}

    def test_inc_dec_get_counter(self):
        """ RSE COUNTER (CORE): Increase, decrease and get counter """
        rse_id = get_rse_id(rse='MOCK', **self.vo)
        rse_update(once=True)
        rse_counter.del_counter(rse_id=rse_id)
        rse_counter.add_counter(rse_id=rse_id)
        cnt = rse_counter.get_counter(rse_id=rse_id)
        del cnt['updated_at']
        assert cnt == {'files': 0, 'bytes': 0}

        count, sum_ = 0, 0
        for i in range(10):
            rse_counter.increase(rse_id=rse_id, files=1, bytes_=2.147e+9)
            rse_update(once=True)
            count += 1
            sum_ += 2.147e+9
            cnt = rse_counter.get_counter(rse_id=rse_id)
            del cnt['updated_at']
            assert cnt == {'files': count, 'bytes': sum_}

        for i in range(4):
            rse_counter.decrease(rse_id=rse_id, files=1, bytes_=2.147e+9)
            rse_update(once=True)
            count -= 1
            sum_ -= 2.147e+9
            cnt = rse_counter.get_counter(rse_id=rse_id)
            del cnt['updated_at']
            assert cnt == {'files': count, 'bytes': sum_}

        for i in range(5):
            rse_counter.increase(rse_id=rse_id, files=1, bytes_=2.147e+9)
            rse_update(once=True)
            count += 1
            sum_ += 2.147e+9
            cnt = rse_counter.get_counter(rse_id=rse_id)
            del cnt['updated_at']
            assert cnt == {'files': count, 'bytes': sum_}

        for i in range(8):
            rse_counter.decrease(rse_id=rse_id, files=1, bytes_=2.147e+9)
            rse_update(once=True)
            count -= 1
            sum_ -= 2.147e+9
            cnt = rse_counter.get_counter(rse_id=rse_id)
            del cnt['updated_at']
            assert cnt == {'files': count, 'bytes': sum_}

    def test_fill_counter_history(self):
        """RSE COUNTER (CORE): Fill the usage history with the current value."""
        db_session = session.get_session()
        db_session.query(models.RSEUsageHistory).delete()
        db_session.commit()
        rse_counter.fill_rse_counter_history_table()
        history_usage = [(usage['rse_id'], usage['files'], usage['source'], usage['used']) for usage in db_session.query(models.RSEUsageHistory)]
        current_usage = [(usage['rse_id'], usage['files'], usage['source'], usage['used']) for usage in db_session.query(models.RSEUsage)]
        for usage in history_usage:
            assert usage in current_usage


@pytest.mark.noparallel(reason='uses pre-defined RSE, fails when run in parallel')
class TestCoreAccountCounter(unittest.TestCase):
    def setUp(self):
        if config_get_bool('common', 'multi_vo', raise_exception=False, default=False):
            self.vo = {'vo': get_vo()}
        else:
            self.vo = {}

    def test_inc_dec_get_counter(self):
        """ACCOUNT COUNTER (CORE): Increase, decrease and get counter """
        account_update(once=True)
        rse_id = get_rse_id(rse='MOCK', **self.vo)
        account = InternalAccount('jdoe', **self.vo)
        account_counter.del_counter(rse_id=rse_id, account=account)
        account_counter.add_counter(rse_id=rse_id, account=account)
        cnt = get_usage(rse_id=rse_id, account=account)
        del cnt['updated_at']
        assert cnt == {'files': 0, 'bytes': 0}

        count, sum_ = 0, 0
        for i in range(10):
            account_counter.increase(rse_id=rse_id, account=account, files=1, bytes_=2.147e+9)
            account_update(once=True)
            count += 1
            sum_ += 2.147e+9
            cnt = get_usage(rse_id=rse_id, account=account)
            del cnt['updated_at']
            assert cnt == {'files': count, 'bytes': sum_}

        for i in range(4):
            account_counter.decrease(rse_id=rse_id, account=account, files=1, bytes_=2.147e+9)
            account_update(once=True)
            count -= 1
            sum_ -= 2.147e+9
            cnt = get_usage(rse_id=rse_id, account=account)
            del cnt['updated_at']
            assert cnt == {'files': count, 'bytes': sum_}

        for i in range(5):
            account_counter.increase(rse_id=rse_id, account=account, files=1, bytes_=2.147e+9)
            account_update(once=True)
            count += 1
            sum_ += 2.147e+9
            cnt = get_usage(rse_id=rse_id, account=account)
            del cnt['updated_at']
            assert cnt == {'files': count, 'bytes': sum_}

        for i in range(8):
            account_counter.decrease(rse_id=rse_id, account=account, files=1, bytes_=2.147e+9)
            account_update(once=True)
            count -= 1
            sum_ -= 2.147e+9
            cnt = get_usage(rse_id=rse_id, account=account)
            del cnt['updated_at']
            assert cnt == {'files': count, 'bytes': sum_}

    def test_fill_counter_history(self):
        """ACCOUNT COUNTER (CORE): Fill the usage history with the current value."""
        db_session = session.get_session()
        db_session.query(models.AccountUsageHistory).delete()
        db_session.commit()
        account_counter.fill_account_counter_history_table()
        history_usage = [(usage['rse_id'], usage['files'], usage['account'], usage['bytes']) for usage in db_session.query(models.AccountUsageHistory)]
        current_usage = [(usage['rse_id'], usage['files'], usage['account'], usage['bytes']) for usage in db_session.query(models.AccountUsage)]
        for usage in history_usage:
            assert usage in current_usage
