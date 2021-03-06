# Copyright (c) 2016 Cisco Systems
# All Rights Reserved.
#
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

from oslo_log import log as logging
import sqlalchemy as sa
from sqlalchemy.sql.expression import func

from aim.db import model_base


LOG = logging.getLogger(__name__)


class Fault(model_base.Base, model_base.AttributeMixin):
    __tablename__ = 'aim_faults'
    __table_args__ = ((sa.Index('idx_aim_faults_status_id', 'status_id'), ) +
                      model_base.to_tuple(model_base.Base.__table_args__))
    status_id = sa.Column(sa.String(255), nullable=False)
    fault_code = sa.Column(sa.String(25), nullable=False)
    severity = sa.Column(sa.String(25), nullable=False)
    description = sa.Column(sa.String(255), default='')
    cause = sa.Column(sa.String(255), default='')
    last_update_timestamp = sa.Column(sa.TIMESTAMP, server_default=func.now(),
                                      onupdate=func.now())
    # external_identifier is an ID used by external entities to easily
    # correlate the fault to the proper external object
    external_identifier = sa.Column(sa.String(255), nullable=False,
                                    primary_key=True)

    def to_attr(self, session):
        """Get resource attribute dictionary for a model object.

        Child classes should override this method to specify a custom
        mapping of model properties to resource attributes.
        """
        result = {}
        for k in dir(self):
            if (not k.startswith('_') and
                    k not in getattr(self, '_exclude_to', []) and
                    not callable(getattr(self, k))):
                if k == 'last_update_timestamp':
                    result[k] = str(self.get_attr(session, k))
                else:
                    result[k] = self.get_attr(session, k)
        return result


class Status(model_base.Base, model_base.HasId, model_base.AttributeMixin):
    """Represents agents running in aim deployments."""

    __tablename__ = 'aim_statuses'
    __table_args__ = (model_base.uniq_column(__tablename__, 'resource_type',
                                             'resource_id') +
                      model_base.to_tuple(model_base.Base.__table_args__))

    resource_type = sa.Column(sa.String(255), nullable=False)
    resource_id = sa.Column(sa.Integer, nullable=False)
    resource_root = model_base.name_column(nullable=False)
    sync_status = sa.Column(sa.String(50), nullable=True)
    sync_message = sa.Column(sa.TEXT, default='')
    health_score = sa.Column(sa.Integer, nullable=False)
