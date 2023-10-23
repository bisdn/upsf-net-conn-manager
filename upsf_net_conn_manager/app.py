# BSD 3-Clause License
#
# Copyright (c) 2023, BISDN GmbH
# All rights reserved.

#!/usr/bin/env python3

"""network connection manager module"""

# pylint: disable=no-member
# pylint: disable=too-many-locals
# pylint: disable=too-many-nested-blocks
# pylint: disable=too-many-statements
# pylint: disable=too-many-branches
# pylint: disable=too-many-lines
# pylint: disable=too-many-instance-attributes
# pylint: disable=access-member-before-definition

import os
import sys
import enum
import time
import hashlib
import logging
import argparse
import socket
import contextlib
import threading
import traceback
from pathlib import (
    Path,
)
import yaml

from upsf_client.upsf import (
    UPSF,
    UpsfError,
)


def str2bool(value):
    """map string to boolean value"""
    if f"{value}".lower() in (
        "t",
        "true",
        "y",
        "yes",
        "1",
    ):
        return True
    return False


class NetConnType(enum.Enum):
    """NetConnType"""

    # unspecified netconn type
    UNSPECIFIED = 0
    # single shard point-to-point
    SS_PTP = 1
    # single shard multipoint-to-point
    SS_MPTP = 2
    # multi shard point-to-point
    MS_PTP = 3
    # multi shard multipoint-to-point
    MS_MPTP = 4


class NetworkConnectionManager(threading.Thread):
    """class NetworkConnectionManager"""

    _defaults = {
        "upsf_host": os.environ.get("UPSF_HOST", "127.0.0.1"),
        "upsf_port": os.environ.get("UPSF_PORT", 50051),
        "nc_type": os.environ.get("NC_TYPE", "ms_mptp"),
        "maximum_supported_quality": os.environ.get("MAXIMUM_SUPPORTED_QUALITY", 0),
        "loglevel": os.environ.get("LOGLEVEL", "info"),
        # Endpoint policy configuration file
        "config_file": os.environ.get("CONFIG_FILE", "/etc/upsf/policy.yml"),
        # periodic background thread: time interval
        "registration_interval": os.environ.get("REGISTRATION_INTERVAL", 60),
        # register shards periodically
        "upsf_auto_register": os.environ.get("UPSF_AUTO_REGISTER", "yes"),
    }

    _loglevels = {
        "critical": logging.CRITICAL,
        "warning": logging.WARNING,
        "info": logging.INFO,
        "error": logging.ERROR,
        "debug": logging.DEBUG,
    }

    def __init__(self, **kwargs):
        """__init__"""
        threading.Thread.__init__(self)
        self._stop_thread = threading.Event()
        self._lock = threading.RLock()
        self._rlock = threading.RLock()
        self.initialize(**kwargs)

    def initialize(self, **kwargs):
        """initialize"""
        for key, value in self._defaults.items():
            setattr(self, key, kwargs.get(key, value))

        # logger
        self._log = logging.getLogger(__name__)
        self._log.setLevel(self._loglevels[self.loglevel])

        # net conn type
        if self.nc_type in (NetConnType.UNSPECIFIED.name.lower(),):
            self.nc_type = NetConnType.UNSPECIFIED.value
        elif self.nc_type in (NetConnType.SS_PTP.name.lower(),):
            self.nc_type = NetConnType.SS_PTP.value
        elif self.nc_type in (NetConnType.SS_MPTP.name.lower(),):
            self.nc_type = NetConnType.SS_MPTP.value
        elif self.nc_type in (NetConnType.MS_PTP.name.lower(),):
            self.nc_type = NetConnType.MS_PTP.value
        elif self.nc_type in (NetConnType.MS_MPTP.name.lower(),):
            self.nc_type = NetConnType.MS_MPTP.value

        self._fp_netconns = {}

        # upsf instance
        self._upsf = UPSF(
            upsf_host=self.upsf_host,
            upsf_port=self.upsf_port,
        )
        # upsf register instance
        self._upsf_register = UPSF(
            upsf_host=self.upsf_host,
            upsf_port=self.upsf_port,
        )

        self.create_default_items()
        self._upsf_auto_register = None
        # create predefined sgups and tsfs
        if str2bool(self.upsf_auto_register):
            self._upsf_auto_register = threading.Thread(
                target=NetworkConnectionManager.upsf_register_task,
                kwargs={
                    "entity": self,
                    "interval": self.registration_interval,
                },
                daemon=True,
            )
            self._upsf_auto_register.start()

        # retrieve existing network connections
        self.netconn_status()

        # mesh
        self.mesh()

    def __str__(self):
        """return simple string"""
        return f"{self.__class__.__name__}()"

    def __repr__(self):
        """return descriptive string"""
        _attributes = "".join(
            [
                f"{key}={getattr(self, key, None)}, "
                for key, value in self._defaults.items()
            ]
        )
        return f"{self.__class__.__name__}({_attributes})"

    @property
    def log(self):
        """return read-only logger"""
        return self._log

    @staticmethod
    def dict_ep2str(**kwargs):
        """map an endpoint specified as dict to string representation"""

        if kwargs.get("type", None) in (
            "Vtep",
            "vtep",
        ):
            return (
                f"name={kwargs.get('name', '')}"
                f"/type=vtep"
                f"/ip_address={kwargs.get('ip_address', '')}"
                f"/udp_port={kwargs.get('udp_port', '')}"
                f"/vni={kwargs.get('vni', '')}"
            )

        if kwargs.get("type", None) in (
            "L2vpn",
            "l2vpn",
        ):
            return (
                f"name={kwargs.get('name', '')}"
                f"/type=l2vpn"
                f"/vpn_id={kwargs.get('vpn_id', '')}"
            )

        if kwargs.get("type", None) in (
            "PortVlan",
            "port_vlan",
        ):
            return (
                f"name={kwargs.get('name', '')}"
                f"/type=port_vlan"
                f"/logical_port={kwargs.get('logical_port', '')}"
                f"/svlan={kwargs.get('svlan', '')}"
                f"/cvlan={kwargs.get('cvlan', '')}"
            )

        return None

    @staticmethod
    def ep2str(endpoint):
        """map an endpoint to string representation"""

        # endpoint type
        ep_type = endpoint.WhichOneof("transport_endpoint")

        if ep_type in (
            "Vtep",
            "vtep",
        ):
            return (
                f"name={endpoint.endpoint_name}"
                f"/type=vtep"
                f"/ip_address={endpoint.vtep.ip_address}"
                f"/udp_port={endpoint.vtep.udp_port}"
                f"/vni={endpoint.vtep.vni}"
            )

        if ep_type in (
            "L2vpn",
            "l2vpn",
        ):
            return (
                f"name={endpoint.endpoint_name}"
                f"/type=l2vpn"
                f"/vpn_id={endpoint.l2vpn.vpn_id}"
            )

        if ep_type in (
            "PortVlan",
            "port_vlan",
        ):
            return (
                f"name={endpoint.endpoint_name}"
                f"/type=port_vlan"
                f"/logical_port={endpoint.port_vlan.logical_port}"
                f"/svlan={endpoint.port_vlan.svlan}"
                f"/cvlan={endpoint.port_vlan.cvlan}"
            )

        return None

    @staticmethod
    def upsf_register_task(**kwargs):
        """periodic background task"""
        while True:
            with contextlib.suppress(RuntimeError):
                # sleep for specified time interval, default: 60s
                time.sleep(int(kwargs.get("interval", 60)))

                if kwargs.get("entity", None) is None:
                    continue

                # send event garbage-collection
                kwargs["entity"].create_default_items()

    def create_default_items(self):
        """register userplanes and tsf from policy file"""
        try:
            if not Path(self.config_file).exists():
                return

            config = None
            with Path(self.config_file).open(encoding="ascii") as file:
                config = yaml.load(file, Loader=yaml.SafeLoader)

            if config is None:
                return

            for kind in ("tsf", "userplane"):
                _kinds = config.get(kind, [])

                for _kind in _kinds:
                    if "name" not in _kind:
                        self.log.warning(
                            {
                                "entity": str(self),
                                "event": "mandatory field not found in endpoint, ignoring",
                                "field": "name",
                                "kind": _kind,
                            }
                        )
                        break
                    name = _kind.get(
                        "name",
                    )
                    _tsf = None
                    _sgup = None
                    if kind in ("userplane",):
                        _sgup = self._upsf_register.get_service_gateway_user_plane(
                            name=name
                        )
                    if kind in ("tsf",):
                        _tsf = self._upsf_register.get_traffic_steering_function(
                            name=name
                        )

                    # params for calling upsf
                    params = {
                        "name": name,
                        "endpoint": [],
                    }

                    for endpoint in _kind.get("endpoints", []):
                        # check for mandatory fields (name, endpoints)

                        self.log.debug(
                            {
                                "entity": str(self),
                                "event": "reading NC policy endpoint from configuration file",
                                "file": self.config_file,
                            }
                        )

                        # check for mandatory fields (type, name, params)
                        for field in (
                            "type",
                            "params",
                        ):
                            if field not in endpoint:
                                self.log.warning(
                                    {
                                        "entity": str(self),
                                        "event": "mandatory field not found in endpoint, ignoring",
                                        "field": field,
                                        "endpoint": endpoint,
                                    }
                                )
                                break
                        else:
                            # default entry
                            _ep = {
                                "type": endpoint["type"],
                                "name": name,
                            }
                            # add endpoint specific parameters
                            _ep.update(endpoint.get("params", {}))

                            # append endpoint
                            if str2bool(endpoint.get("default", "no")) is False:
                                params["endpoint"].append(_ep)
                            # prepend endpoint
                            else:
                                params["endpoint"] = [_ep] + params["endpoint"]

                    if kind in ("userplane",):
                        # user plane specific parameters
                        for field, value in (
                            ("service_gateway_name", None),
                            ("supported_service_group", []),
                            ("max_shards", None),
                            ("max_session_count", None),
                        ):
                            params[field] = _kind.get(field, value)

                        # create new sgup
                        if _sgup is None:
                            self.log.info(
                                {
                                    "entity": str(self),
                                    "event": "register (create) sgup at upsf",
                                    "params": params,
                                }
                            )
                            self._upsf_register.create_service_gateway_user_plane(
                                **params,
                            ).service_gateway_user_plane

                        # update existing sgup
                        else:
                            self.log.debug(
                                {
                                    "entity": str(self),
                                    "event": "skipping register (update) existing sgup at upsf",
                                    "params": params,
                                }
                            )
                            continue

                    if kind in ("tsf",):
                        # create new tsf
                        if _tsf is None:
                            self.log.info(
                                {
                                    "entity": str(self),
                                    "event": "register (create) tsf at upsf",
                                    "params": params,
                                }
                            )

                            self._upsf_register.create_traffic_steering_function(
                                **params,
                            ).traffic_steering_function

                        # update existing tsf
                        else:
                            self.log.info(
                                {
                                    "entity": str(self),
                                    "event": "skipping register (update) tsf at upsf",
                                    "params": params,
                                }
                            )
                            continue

        except UpsfError as error:
            self.log.error(
                {
                    "entity": str(self),
                    "event": "error occurred",
                    "error": str(error),
                }
            )

    def netconn_status(self):
        """retrieve all active network connections
        and calculate fingerprint for all tunnel endpoints
        """
        try:
            # actual list of network connections
            netconns = {
                netconn.name: netconn
                for netconn in self._upsf.list_network_connections()
            }

            # remove old state
            self._fp_netconns.clear()

            # for all network connections
            for _, netconn in netconns.items():
                # list of sgup endpoints in this network connection
                sgup_endpoints = []

                # and a list of tsf endpoints
                tsf_endpoints = []

                # network connection spec type
                nc_spec_type = netconn.spec.WhichOneof("nc_spec")

                if nc_spec_type in (
                    "SsPtpSpec",
                    "ss_ptp",
                ):
                    # multiple sgup endpoints
                    for endpoint in netconn.spec.ss_ptp.sgup_endpoint:
                        sgup_endpoints.append(NetworkConnectionManager.ep2str(endpoint))

                    # single tsf endpoint
                    tsf_endpoints.append(
                        NetworkConnectionManager.ep2str(
                            netconn.spec.ss_ptp.tsf_endpoint
                        )
                    )

                elif nc_spec_type in (
                    "SsMptpSpec",
                    "ss_mptp",
                ):
                    # multiple sgup endpoints
                    for endpoint in netconn.spec.ss_mptpc.sgup_endpoint:
                        sgup_endpoints.append(NetworkConnectionManager.ep2str(endpoint))

                    # multiple tsf endpoints
                    for endpoint in netconn.spec.ss_mptpc.tsf_endpoint:
                        tsf_endpoints.append(NetworkConnectionManager.ep2str(endpoint))

                elif nc_spec_type in (
                    "MsPtpSpec",
                    "ms_ptp",
                ):
                    # single sgup endpoint
                    sgup_endpoints.append(
                        NetworkConnectionManager.ep2str(
                            netconn.spec.ms_ptp.sgup_endpoint
                        )
                    )

                    # single tsf endpoint
                    tsf_endpoints.append(
                        NetworkConnectionManager.ep2str(
                            netconn.spec.ms_ptp.tsf_endpoint
                        )
                    )

                elif nc_spec_type in (
                    "MsMptpSpec",
                    "ms_mptp",
                ):
                    # single sgup endpoint
                    sgup_endpoints.append(
                        NetworkConnectionManager.ep2str(
                            netconn.spec.ms_mptp.sgup_endpoint
                        )
                    )

                    # multiple tsf endpoints
                    for endpoint in netconn.spec.ms_mptp.tsf_endpoint:
                        tsf_endpoints.append(NetworkConnectionManager.ep2str(endpoint))

                # cleanup endpoint lists
                sgup_endpoints = [
                    endpoint
                    for endpoint in sgup_endpoints
                    if endpoint
                    not in (
                        "",
                        None,
                    )
                ]
                tsf_endpoints = [
                    endpoint
                    for endpoint in tsf_endpoints
                    if endpoint
                    not in (
                        "",
                        None,
                    )
                ]

                # sanity check: endpoint lists must not be empty
                if len(sgup_endpoints) == 0 or len(tsf_endpoints) == 0:
                    self.log.warning(
                        {
                            "entity": str(self),
                            "event": "no valid endpoints for network connection, ignoring",
                            "netconn.name": netconn.name,
                            "sgup_endpoints": sgup_endpoints,
                            "tsf_endpoints": tsf_endpoints,
                        }
                    )
                    continue

                # calculate fingerprint
                fingerprint = hashlib.sha256(
                    ",".join(sgup_endpoints).encode("ascii")
                    + ",".join(tsf_endpoints).encode("ascii")
                ).hexdigest()

                # store fingerprint
                self._fp_netconns[netconn.name] = fingerprint

                self.log.info(
                    {
                        "entity": str(self),
                        "event": "add netconn fingerprint",
                        "netconn.name": netconn.name,
                        "fingerprint": fingerprint,
                        "sgup_endpoints": sgup_endpoints,
                        "tsf_endpoints": tsf_endpoints,
                    }
                )

        except UpsfError as error:
            self.log.error(
                {
                    "entity": str(self),
                    "event": "error occurred",
                    "error": error,
                }
            )

    @staticmethod
    def netconn_fingerprint(sgup_endpoint, tsf_endpoint):
        """calculate network connection fingerprint"""

        # list of sgup endpoints in this network connection
        sgup_endpoints = []

        # and a list of tsf endpoints
        tsf_endpoints = []

        if isinstance(sgup_endpoint, (dict,)):
            sgup_endpoints.append(NetworkConnectionManager.dict_ep2str(**sgup_endpoint))
        elif isinstance(
            sgup_endpoint,
            (
                list,
                tuple,
            ),
        ):
            for item in sgup_endpoint:
                sgup_endpoints.append(NetworkConnectionManager.dict_ep2str(**item))

        if isinstance(tsf_endpoint, (dict,)):
            tsf_endpoints.append(NetworkConnectionManager.dict_ep2str(**tsf_endpoint))
        elif isinstance(
            tsf_endpoint,
            (
                list,
                tuple,
            ),
        ):
            for item in tsf_endpoint:
                tsf_endpoints.append(NetworkConnectionManager.dict_ep2str(**item))

        # cleanup endpoint lists
        sgup_endpoints = [
            endpoint
            for endpoint in sgup_endpoints
            if endpoint
            not in (
                "",
                None,
            )
        ]
        tsf_endpoints = [
            endpoint
            for endpoint in tsf_endpoints
            if endpoint
            not in (
                "",
                None,
            )
        ]

        # sanity check: endpoint lists must not be empty
        if len(sgup_endpoints) == 0 or len(tsf_endpoints) == 0:
            return None

        # calculate fingerprint
        fingerprint = hashlib.sha256(
            ",".join(sgup_endpoints).encode("ascii")
            + ",".join(tsf_endpoints).encode("ascii")
        ).hexdigest()

        return fingerprint

    def mesh(self):
        """create network connections"""

        # get status (fingerprints) of existing network connections
        self.netconn_status()

        if self.nc_type in (NetConnType.UNSPECIFIED.value,):
            self.log.warning(
                {
                    "entity": str(self),
                    "event": "mesh: net conn type not specified, ignoring",
                }
            )
            return

        if self.nc_type in (NetConnType.SS_PTP.value,):
            self.log.warning(
                {
                    "entity": str(self),
                    "event": "mesh: net conn type ss-ptp not supported yet, ignoring",
                }
            )
            return

        if self.nc_type in (NetConnType.SS_MPTP.value,):
            self.log.warning(
                {
                    "entity": str(self),
                    "event": "mesh: net conn type ss-mptp not supported yet, ignoring",
                }
            )
            return

        if self.nc_type in (NetConnType.MS_PTP.value,):
            self.mesh_ms_ptp()

        elif self.nc_type in (NetConnType.MS_MPTP.value,):
            self.mesh_ms_mptp()

    def mesh_ms_ptp(self):
        """create one netconn per (UP, TSF) pair"""

        try:
            current_ncs = set()
            previous_ncs = set(nc.name for nc in self._upsf.list_network_connections())

            # for each user plane ...
            for uplane in self._upsf.list_service_gateway_user_planes():
                sgup_endpoint = None

                # endpoint must exist
                if uplane.spec.default_endpoint.WhichOneof("transport_endpoint") in (
                    "",
                ):
                    self.log.warning(
                        {
                            "entity": str(self),
                            "event": "SGUP: No Endpoint provided, skipping",
                        }
                    )
                    continue

                # endpoint must be of type vtep (for now)
                if uplane.spec.default_endpoint.WhichOneof(
                    "transport_endpoint"
                ) not in ("vtep",):
                    self.log.warning(
                        {
                            "entity": str(self),
                            "event": "SGUP: Only Vtep Endpoints are supported "
                            "at the moment, skipping",
                            "endpoint_type": uplane.spec.default_endpoint.WhichOneof(
                                "transport_endpoint"
                            ),
                        }
                    )
                    continue

                # endpoint must contain a vtep:ip_address
                if uplane.spec.default_endpoint.vtep.ip_address in (
                    "",
                    None,
                ):
                    self.log.warning(
                        {
                            "entity": str(self),
                            "event": "UP: IP address not defined, skipping",
                            "ip_address_spec": uplane.spec.default_endpoint.vtep.ip_address,
                        }
                    )
                    continue

                # endpoint must contain a vtep:udp_port
                if uplane.spec.default_endpoint.vtep.udp_port in (
                    0,
                    None,
                ):
                    self.log.warning(
                        {
                            "entity": str(self),
                            "event": "UP: UDP port not defined, skipping",
                            "udp_port_spec": uplane.spec.default_endpoint.vtep.udp_port,
                        }
                    )
                    continue

                sgup_endpoint = {
                    "type": "vtep",
                    "name": uplane.spec.default_endpoint.endpoint_name,
                    "ip_address": uplane.spec.default_endpoint.vtep.ip_address,
                    "udp_port": int(uplane.spec.default_endpoint.vtep.udp_port),
                    "vni": int(uplane.spec.default_endpoint.vtep.vni),
                }

                # and for each traffic steering function ...
                for tsf in self._upsf.list_traffic_steering_functions():
                    tsf_endpoint = None

                    if tsf.spec.default_endpoint.WhichOneof("transport_endpoint") in (
                        "",
                    ):
                        self.log.warning(
                            {
                                "entity": str(self),
                                "event": "TSF: No Endpoint provided, skipping",
                                "tsf.name": tsf.name,
                            }
                        )
                        continue

                    if tsf.spec.default_endpoint.WhichOneof(
                        "transport_endpoint"
                    ) not in ("vtep",):
                        self.log.warning(
                            {
                                "entity": str(self),
                                "event": "TSF: Only Vtep Endpoints are supported "
                                "at the moment, skipping",
                                "endpoint_type": tsf.spec.default_endpoint.WhichOneof(
                                    "transport_endpoint"
                                ),
                            }
                        )
                        continue

                    if tsf.spec.default_endpoint.vtep.ip_address in (
                        "",
                        None,
                    ):
                        self.log.warning(
                            {
                                "entity": str(self),
                                "event": "TSF: IP address not defined, skipping",
                                "ip_address_spec": tsf.spec.default_endpoint.vtep.ip_address,
                            }
                        )
                        continue

                    if tsf.spec.default_endpoint.vtep.udp_port in (
                        0,
                        None,
                    ):
                        self.log.warning(
                            {
                                "entity": str(self),
                                "event": "TSF: UDP port not defined, skipping",
                                "udp_port_spec": tsf.spec.default_endpoint.vtep.udp_port,
                            }
                        )
                        continue

                    # VxLAN VNI used by SGUP and TSF must match
                    if (
                        tsf.spec.default_endpoint.vtep.vni
                        != uplane.spec.default_endpoint.vtep.vni
                    ):
                        self.log.warning(
                            {
                                "entity": str(self),
                                "event": "VxLAN VNI mismatch between SGUP and TSF, ignoring",
                                "tsf.vni": tsf.spec.default_endpoint.vtep.vni,
                                "up.vni": uplane.spec.default_endpoint.vtep.vni,
                            }
                        )
                        continue

                    tsf_endpoint = {
                        "type": "vtep",
                        "name": tsf.spec.default_endpoint.endpoint_name,
                        "ip_address": tsf.spec.default_endpoint.vtep.ip_address,
                        "udp_port": int(tsf.spec.default_endpoint.vtep.udp_port),
                        "vni": int(tsf.spec.default_endpoint.vtep.vni),
                    }

                    nc_active = {
                        uplane.spec.default_endpoint.endpoint_name: "true",
                        tsf.spec.default_endpoint.endpoint_name: "false",
                    }

                    #################################################################
                    # create network connection between single UP and single TSF instance
                    #
                    try:
                        # calculate sha256 hash for up_name
                        nc_name = hashlib.sha256(
                            uplane.name.encode("ascii") + tsf.name.encode("ascii")
                        ).hexdigest()

                        # sanity check: sgup_endpoint and tsf_endpoint must not be None
                        if sgup_endpoint is None or tsf_endpoint is None:
                            self.log.warning(
                                {
                                    "entity": str(self),
                                    "event": "no valid endpoints for network connection, ignoring",
                                    "netconn.name": nc_name,
                                    "sgup_endpoint": sgup_endpoint,
                                    "tsf_endpoint": tsf_endpoint,
                                }
                            )
                            continue

                        nc_fingerprint = self.netconn_fingerprint(
                            [sgup_endpoint], [tsf_endpoint]
                        )

                        self.log.info(
                            {
                                "entity": str(self),
                                "event": "calculate net-conn ms-ptp up to tsf",
                                "up.name": uplane.name,
                                "tsf.name": tsf.name,
                                "nc.name": nc_name,
                                "nc.fingerprint": nc_fingerprint,
                                "nc.fingerprint.prev": self._fp_netconns.get(
                                    nc_name, ""
                                ),
                            }
                        )

                        # parameters for netconn of type MS-PTP
                        # many netconns between (UP, TSF) pairs
                        params = {
                            "nc_active": nc_active,
                            "name": nc_name,
                            "sgup_endpoints": [sgup_endpoint],
                            "tsf_endpoints": [tsf_endpoint],
                            "nc_spec_type": "ms_ptp",
                            "allocated_shards": 0,
                            "maximum_supported_quality": self.maximum_supported_quality,
                            "list_merge_strategy": "replace",
                        }

                        # search network connection with name == fingerprint
                        if self._upsf.get_network_connection(name=nc_name):
                            # update existing nc
                            try:
                                # fingerprint has changed?
                                if self._fp_netconns[nc_name] != nc_fingerprint:
                                    self._upsf.update_network_connection(**params)

                                # add nc to set of ephemeral set of current ncs
                                current_ncs.add(nc_name)

                            except UpsfError as error:
                                self.log.error(
                                    {
                                        "entity": str(self),
                                        "event": "error updating network connection",
                                        "error": error,
                                        "traceback": traceback.format_exc(),
                                    }
                                )

                        # if none exists, create new network connection in upsf
                        else:
                            try:
                                self._upsf.create_network_connection(**params)

                                # add nc to set of ephemeral set of current ncs
                                current_ncs.add(nc_name)

                            except UpsfError as error:
                                self.log.error(
                                    {
                                        "entity": str(self),
                                        "event": "error creating network connection",
                                        "error": error,
                                        "traceback": traceback.format_exc(),
                                    }
                                )

                    except UpsfError as error:
                        self.log.error(
                            {
                                "entity": str(self),
                                "event": "error in network mesh",
                                "error": error,
                                "traceback": traceback.format_exc(),
                            }
                        )

            # remove any network connection not present in current_ncs
            ncs = previous_ncs - current_ncs
            self.log.info(
                {
                    "entity": str(self),
                    "event": "determine network connections for deletion",
                    "previous set of nc-ids": previous_ncs,
                    "current set of nc-ids": current_ncs,
                    "to be deleted set of nc-ids": ncs,
                }
            )

            for nc_name in ncs:
                self.log.info(
                    {
                        "entity": str(self),
                        "event": "delete network connection",
                        "nc.name": nc_name,
                    }
                )
                try:
                    self._upsf.delete_network_connection(
                        name=nc_name,
                    )
                except UpsfError as error:
                    self.log.error(
                        {
                            "entity": str(self),
                            "event": "error deleting network connection",
                            "error": error,
                            "traceback": traceback.format_exc(),
                        }
                    )

        except UpsfError as error:
            self.log.error(
                {
                    "entity": str(self),
                    "event": "mesh, error occurred",
                    "error": error,
                    "traceback": traceback.format_exc(),
                }
            )

    def mesh_ms_mptp(self):
        """create one netconn per UP connected to all TSFs"""

        try:
            current_ncs = set()
            previous_ncs = set(nc.name for nc in self._upsf.list_network_connections())
            # iterate over all user planes
            #
            # for up_name, up in self._service_gateway_user_planes.items():
            for uplane in self._upsf.list_service_gateway_user_planes():
                up_name = uplane.name

                nc_active = {}

                # endpoint must exist
                if uplane.spec.default_endpoint.WhichOneof("transport_endpoint") in (
                    "",
                ):
                    self.log.warning(
                        {
                            "entity": str(self),
                            "event": "SGUP: No Endpoint provided, skipping",
                        }
                    )
                    continue

                # endpoint must be of type vtep (for now)
                if uplane.spec.default_endpoint.WhichOneof(
                    "transport_endpoint"
                ) not in ("vtep",):
                    self.log.warning(
                        {
                            "entity": str(self),
                            "event": "SGUP: Only Vtep Endpoints are supported "
                            "at the moment, skipping",
                            "endpoint_type": uplane.spec.default_endpoint.WhichOneof(
                                "transport_endpoint"
                            ),
                        }
                    )
                    continue

                # endpoint must contain a vtep:ip_address
                if uplane.spec.default_endpoint.vtep.ip_address in (
                    "",
                    None,
                ):
                    self.log.warning(
                        {
                            "entity": str(self),
                            "event": "UP: IP address not defined, skipping",
                            "ip_address_spec": uplane.spec.default_endpoint.vtep.ip_address,
                        }
                    )
                    continue

                # endpoint must contain a vtep:udp_port
                if uplane.spec.default_endpoint.vtep.udp_port in (
                    0,
                    None,
                ):
                    self.log.warning(
                        {
                            "entity": str(self),
                            "event": "UP: UDP port not defined, skipping",
                            "udp_port_spec": uplane.spec.default_endpoint.vtep.udp_port,
                        }
                    )
                    continue

                sgup_endpoint = {
                    "type": "vtep",
                    "name": uplane.spec.default_endpoint.endpoint_name,
                    "ip_address": uplane.spec.default_endpoint.vtep.ip_address,
                    "udp_port": int(uplane.spec.default_endpoint.vtep.udp_port),
                    "vni": int(uplane.spec.default_endpoint.vtep.vni),
                }
                nc_active.update(
                    {
                        uplane.spec.default_endpoint.endpoint_name: "true",
                    }
                )

                # bind each TSF to current UP
                #
                tsf_endpoints = []
                # for tsf_name, tsf in self._traffic_steering_functions.items():
                for tsf in self._upsf.list_traffic_steering_functions():
                    if tsf.spec.default_endpoint.WhichOneof("transport_endpoint") in (
                        "",
                    ):
                        self.log.warning(
                            {
                                "entity": str(self),
                                "event": "TSF: No Endpoint provided, skipping",
                                "tsf.name": tsf.name,
                            }
                        )
                        continue

                    if tsf.spec.default_endpoint.WhichOneof(
                        "transport_endpoint"
                    ) not in ("vtep",):
                        self.log.warning(
                            {
                                "entity": str(self),
                                "event": "TSF: Only Vtep Endpoints are supported "
                                "at the moment, skipping",
                                "endpoint_type": tsf.spec.default_endpoint.WhichOneof(
                                    "transport_endpoint"
                                ),
                            }
                        )
                        continue

                    if tsf.spec.default_endpoint.vtep.ip_address in (
                        "",
                        None,
                    ):
                        self.log.warning(
                            {
                                "entity": str(self),
                                "event": "TSF: IP address not defined, skipping",
                                "ip_address_spec": tsf.spec.default_endpoint.vtep.ip_address,
                            }
                        )
                        continue

                    if tsf.spec.default_endpoint.vtep.udp_port in (
                        0,
                        None,
                    ):
                        self.log.warning(
                            {
                                "entity": str(self),
                                "event": "TSF: UDP port not defined, skipping",
                                "udp_port_spec": tsf.spec.default_endpoint.vtep.udp_port,
                            }
                        )
                        continue

                    # VxLAN VNI used by SGUP and TSF must match
                    if (
                        tsf.spec.default_endpoint.vtep.vni
                        != uplane.spec.default_endpoint.vtep.vni
                    ):
                        self.log.warning(
                            {
                                "entity": str(self),
                                "event": "VxLAN VNI mismatch between SGUP and TSF, ignoring",
                                "tsf.vni": tsf.spec.default_endpoint.vtep.vni,
                                "up.vni": uplane.spec.default_endpoint.vtep.vni,
                            }
                        )
                        continue

                    tsf_endpoints.append(
                        {
                            "type": "vtep",
                            "name": tsf.spec.default_endpoint.endpoint_name,
                            "ip_address": tsf.spec.default_endpoint.vtep.ip_address,
                            "udp_port": int(tsf.spec.default_endpoint.vtep.udp_port),
                            "vni": int(tsf.spec.default_endpoint.vtep.vni),
                        }
                    )
                    nc_active.update(
                        {
                            tsf.spec.default_endpoint.endpoint_name: "false",
                        }
                    )

                if len(tsf_endpoints) == 0:
                    self.log.warning(
                        {
                            "entity": str(self),
                            "event": "no TSF endpoints found, ignoring",
                            "up": up_name,
                        }
                    )
                    continue

                # create network connection towards current UP
                #
                try:
                    # calculate sha256 hash for up_name
                    nc_name = hashlib.sha256(uplane.name.encode("ascii")).hexdigest()

                    # sanity check: sgup_endpoint and tsf_endpoints must not be None or empty
                    if sgup_endpoint is None or len(tsf_endpoints) == 0:
                        self.log.warning(
                            {
                                "entity": str(self),
                                "event": "no valid endpoints for network connection, ignoring",
                                "netconn.name": nc_name,
                                "sgup_endpoint": sgup_endpoint,
                                "tsf_endpoints": tsf_endpoints,
                            }
                        )
                        continue

                    nc_fingerprint = self.netconn_fingerprint(
                        [sgup_endpoint], tsf_endpoints
                    )

                    self.log.info(
                        {
                            "entity": str(self),
                            "event": "calculate network connection to up",
                            "up.name": uplane.name,
                            "nc.name": nc_name,
                            "nc.fingerprint": nc_fingerprint,
                            "nc.fingerprint.prev": self._fp_netconns.get(nc_name, ""),
                        }
                    )

                    # parameters for netconn of type MS-MPTP
                    # many TSF instances to one UP instance
                    params = {
                        "nc_active": nc_active,
                        "name": nc_name,
                        "sgup_endpoints": [sgup_endpoint],
                        "tsf_endpoints": tsf_endpoints,
                        "nc_spec_type": "ms_mptp",
                        "allocated_shards": 0,
                        "maximum_supported_quality": self.maximum_supported_quality,
                        "list_merge_strategy": "replace",
                    }

                    # search network connection with name == fingerprint
                    if self._upsf.get_network_connection(name=nc_name):
                        # update existing nc
                        try:
                            if self._fp_netconns[nc_name] != nc_fingerprint:
                                self._upsf.update_network_connection(**params)

                            # add nc to set of ephemeral set of current ncs
                            current_ncs.add(nc_name)

                        except UpsfError as error:
                            self.log.error(
                                {
                                    "entity": str(self),
                                    "event": "error updating network connection",
                                    "error": error,
                                    "traceback": traceback.format_exc(),
                                }
                            )

                    # if none exists, create new network connection in upsf
                    else:
                        try:
                            self._upsf.create_network_connection(**params)

                            # add nc to set of ephemeral set of current ncs
                            current_ncs.add(nc_name)

                        except UpsfError as error:
                            self.log.error(
                                {
                                    "entity": str(self),
                                    "event": "error creating network connection",
                                    "error": error,
                                    "traceback": traceback.format_exc(),
                                }
                            )

                except UpsfError as error:
                    self.log.error(
                        {
                            "entity": str(self),
                            "event": "error in network mesh",
                            "error": error,
                            "traceback": traceback.format_exc(),
                        }
                    )

            # remove any network connection not present in current_ncs
            ncs = previous_ncs - current_ncs
            self.log.info(
                {
                    "entity": str(self),
                    "event": "determine network connections for deletion",
                    "previous set of nc-ids": previous_ncs,
                    "current set of nc-ids": current_ncs,
                    "to be deleted set of nc-ids": ncs,
                }
            )
            for nc_name in ncs:
                self.log.info(
                    {
                        "entity": str(self),
                        "event": "delete network connection",
                        "nc.name": nc_name,
                    }
                )
                try:
                    self._upsf.delete_network_connection(
                        name=nc_name,
                    )
                except UpsfError as error:
                    self.log.error(
                        {
                            "entity": str(self),
                            "event": "error deleting network connection",
                            "error": error,
                            "traceback": traceback.format_exc(),
                        }
                    )

        except UpsfError as error:
            self.log.error(
                {
                    "entity": str(self),
                    "event": "mesh, error occurred",
                    "error": error,
                    "traceback": traceback.format_exc(),
                }
            )

    def stop(self):
        """signals background thread a stop condition"""
        self.log.debug(
            {
                "entity": str(self),
                "event": "thread terminating ...",
            }
        )
        self._stop_thread.set()
        self.join()

    def run(self):
        """runs main loop as background thread"""
        while not self._stop_thread.is_set():
            with contextlib.suppress(Exception):
                try:
                    upsf_subscriber = UPSF(
                        upsf_host=self.upsf_host,
                        upsf_port=self.upsf_port,
                    )
                    print(upsf_subscriber)

                    for item in upsf_subscriber.read(
                        # subscribe to up, tsf
                        itemtypes=[
                            "service_gateway_user_plane",
                            "traffic_steering_function",
                        ],
                        watch=True,
                    ):
                        try:
                            # service gateway user planes
                            if item.service_gateway_user_plane.name not in ("",):
                                self.mesh()

                            # traffic steering functions
                            elif item.traffic_steering_function.name not in ("",):
                                self.mesh()

                        except UpsfError as error:
                            self.log.error(
                                {
                                    "entity": str(self),
                                    "event": "error occurred",
                                    "error": error,
                                }
                            )

                        if self._stop_thread.is_set():
                            break

                except UpsfError as error:
                    self.log.error(
                        {
                            "entity": str(self),
                            "event": "error occurred",
                            "error": error,
                        }
                    )
                    time.sleep(1)


def parse_arguments(defaults, loglevels):
    """parse command line arguments"""
    parser = argparse.ArgumentParser(sys.argv[0])

    parser.add_argument(
        "--upsf-host",
        help=f'upsf grpc host (default: {defaults["upsf_host"]})',
        dest="upsf_host",
        action="store",
        default=defaults["upsf_host"],
        type=str,
    )

    parser.add_argument(
        "--upsf-port",
        "-p",
        help=f'upsf grpc port (default: {defaults["upsf_port"]})',
        dest="upsf_port",
        action="store",
        default=defaults["upsf_port"],
        type=int,
    )

    parser.add_argument(
        "--nc-type",
        help=f'netconn type (default: {defaults["nc_type"]})',
        dest="nc_type",
        action="store",
        choices=[
            "ss_ptp",
            "ss_mptp",
            "ms_ptp",
            "ms_mptp",
        ],
        default=defaults["nc_type"],
        type=str,
    )

    parser.add_argument(
        "--maximum-supported-quality",
        "--qos",
        "-q",
        help="maximum supported quality for network connections "
        f'(default: {defaults["maximum_supported_quality"]})',
        dest="maximum_supported_quality",
        action="store",
        default=defaults["maximum_supported_quality"],
        type=int,
    )

    parser.add_argument(
        "--config-file",
        "--conf",
        "-c",
        help=f'NC configuration file (default: {defaults["config_file"]})',
        dest="config_file",
        action="store",
        default=defaults["config_file"],
        type=str,
    )

    parser.add_argument(
        "--loglevel",
        "-l",
        help=f'set log level (default: {defaults["loglevel"]})',
        dest="loglevel",
        choices=loglevels.keys(),
        action="store",
        default=defaults["loglevel"],
        type=str,
    )

    return parser.parse_args(sys.argv[1:])


def main():
    """main routine"""
    defaults = {
        # upsf host, default: 127.0.0.1
        "upsf_host": os.environ.get("UPSF_HOST", "127.0.0.1"),
        # upsf port, default: 50051
        "upsf_port": os.environ.get("UPSF_PORT", 50051),
        # netconn type, default: ms_ptp
        "nc_type": os.environ.get("NC_TYPE", "ms_mptp"),
        # maximum-supported-quality, default: 0
        "maximum_supported_quality": os.environ.get("MAXIMUM_SUPPORTED_QUALITY", 0),
        # loglevel, default: 'info'
        "loglevel": os.environ.get("LOGLEVEL", "info"),
        # Endpoint policy configuration file
        "config_file": os.environ.get("CONFIG_FILE", "/etc/upsf/policy.yml"),
        # periodic background thread: time interval
        "registration_interval": os.environ.get("REGISTRATION_INTERVAL", 60),
        # register shards periodically
        "upsf_auto_register": os.environ.get("UPSF_AUTO_REGISTER", "yes"),
    }

    loglevels = {
        "info": logging.INFO,
        "warning": logging.WARNING,
        "error": logging.ERROR,
        "critical": logging.CRITICAL,
        "debug": logging.DEBUG,
    }

    args = parse_arguments(defaults, loglevels)

    # configure logging, here: root logger
    log = logging.getLogger("")

    # add StreamHandler
    hnd = logging.StreamHandler()
    formatter = logging.Formatter(
        f"%(asctime)s: [%(levelname)s] host: {socket.gethostname()}, "
        f"process: {sys.argv[0]}, "
        "module: %(module)s, "
        "func: %(funcName)s, "
        "trace: %(exc_text)s, "
        "message: %(message)s"
    )
    hnd.setFormatter(formatter)
    hnd.setLevel(loglevels[args.loglevel])
    log.addHandler(hnd)

    # set log level of root logger
    log.setLevel(loglevels[args.loglevel])

    kwargs = vars(args)
    log.debug(kwargs)

    ncmgr = NetworkConnectionManager(**kwargs)
    ncmgr.start()
    while True:
        try:
            time.sleep(1)
        except KeyboardInterrupt:
            break


if __name__ == "__main__":
    main()
