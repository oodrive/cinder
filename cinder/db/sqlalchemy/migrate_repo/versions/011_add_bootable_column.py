#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.


from sqlalchemy import Boolean, Column, MetaData, Table


def upgrade(migrate_engine):
    """Add bootable column to volumes."""
    meta = MetaData()
    meta.bind = migrate_engine

    volumes = Table('volumes', meta, autoload=True)
    bootable = Column('bootable', Boolean)

    volumes.create_column(bootable)
    volumes.update().values(bootable=False).execute()

    glance_metadata = Table('volume_glance_metadata', meta, autoload=True)
    glance_items = list(glance_metadata.select().execute())
    for item in glance_items:
        volumes.update().\
            where(volumes.c.id == item['volume_id']).\
            values(bootable=True).execute()


def downgrade(migrate_engine):
    """Remove bootable column to volumes."""
    meta = MetaData()
    meta.bind = migrate_engine

    volumes = Table('volumes', meta, autoload=True)
    bootable = volumes.columns.bootable
    #bootable = Column('bootable', Boolean)
    volumes.drop_column(bootable)
