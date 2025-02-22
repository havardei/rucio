# -*- coding: utf-8 -*-
# Copyright CERN since 2015
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

''' change tokens pk '''

from alembic import context
from alembic.op import create_primary_key, create_foreign_key, drop_constraint


# Alembic revision identifiers
revision = '2eef46be23d4'
down_revision = '58c8b78301ab'


def upgrade():
    '''
    Upgrade the database to this revision
    '''

    if context.get_context().dialect.name in ['oracle', 'mysql', 'postgresql']:
        drop_constraint('TOKENS_ACCOUNT_FK', 'tokens', type_='foreignkey')
        drop_constraint('TOKENS_PK', 'tokens', type_='primary')
        create_primary_key('TOKENS_PK', 'tokens', ['token'])
        create_foreign_key('TOKENS_ACCOUNT_FK', 'tokens', 'accounts', ['account'], ['account'])


def downgrade():
    '''
    Downgrade the database to the previous revision
    '''

    if context.get_context().dialect.name in ['oracle', 'mysql', 'postgresql']:
        drop_constraint('TOKENS_ACCOUNT_FK', 'tokens', type_='foreignkey')
        drop_constraint('TOKENS_PK', 'tokens', type_='primary')
        create_primary_key('TOKENS_PK', 'tokens', ['account', 'token'])
        create_foreign_key('TOKENS_ACCOUNT_FK', 'tokens', 'accounts', ['account'], ['account'])
