# -*- coding: utf-8 -*-
# Copyright CERN since 2012
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

import rucio.api.permission
import rucio.common.exception

from rucio.core import scope as core_scope
from rucio.common.types import InternalAccount, InternalScope
from rucio.common.schema import validate_schema


def list_scopes(filter_={}, vo='def'):
    """
    Lists all scopes.

    :param filter_: Dictionary of attributes by which the input data should be filtered
    :param vo: The VO to act on.

    :returns: A list containing all scopes.
    """
    # If filter is empty, create a new dict to avoid overwriting the function's default
    if not filter_:
        filter_ = {}

    if 'scope' in filter_:
        filter_['scope'] = InternalScope(scope=filter_['scope'], vo=vo)
    else:
        filter_['scope'] = InternalScope(scope='*', vo=vo)
    return [scope.external for scope in core_scope.list_scopes(filter_=filter_)]


def add_scope(scope, account, issuer, vo='def'):
    """
    Creates a scope for an account.

    :param account: The account name.
    :param scope: The scope identifier.
    :param issuer: The issuer account.
    :param vo: The VO to act on.
    """

    validate_schema(name='scope', obj=scope, vo=vo)

    kwargs = {'scope': scope, 'account': account}
    if not rucio.api.permission.has_permission(issuer=issuer, vo=vo, action='add_scope', kwargs=kwargs):
        raise rucio.common.exception.AccessDenied('Account %s can not add scope' % (issuer))

    scope = InternalScope(scope, vo=vo)
    account = InternalAccount(account, vo=vo)

    core_scope.add_scope(scope, account)


def get_scopes(account, vo='def'):
    """
    Gets a list of all scopes for an account.

    :param account: The account name.
    :param vo: The VO to act on.

    :returns: A list containing the names of all scopes for this account.
    """

    account = InternalAccount(account, vo=vo)

    return [scope.external for scope in core_scope.get_scopes(account)]
