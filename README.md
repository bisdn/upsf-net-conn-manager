# upsf-net-conn-manager

This repository contains a Python based application named **net-conn
manager** for managing network connections within a BBF WT-474
compliant User Plane Selection Function (UPSF).

Its main purpose is to subscribe and listen on events emitted by the
UPSF via its gRPC streaming interface, and to create point-to-point
network connections between Traffic Steering Functions (TSF) and
Service Gateway User Planes (SGUP) on the fly.

In addition, net-conn manager may be used for creating SGUP and TSF
entities within the UPSF for testing purposes. It runs a periodic
background task that reads pre-defined SGUP and TSF instances
from an associated policy file and if missing in the UPSF, creates
those instances.

See next table for a list of command line options supported by 
net-conn manager. An associated environment variable exists for each
command line option: the CLI option takes precedence, though.

<table>
  <tr>
    <th>Option</th>
    <th>Default value</th>
    <th>Environment variable</th>
    <th>Description</th>
  </tr>
  <tr>
    <td>--upsf-host</td>
    <td>127.0.0.1</td>
    <td>UPSF_HOST</td>
    <td>UPSF server host to connect to</td>
  </tr>
  <tr>
    <td>--upsf-port</td>
    <td>50051</td>
    <td>UPSF_PORT</td>
    <td>UPSF server port to connect to</td>
  </tr>
  <tr>
    <td>--nc-type</td>
    <td>ms_mptp</td>
    <td>NC_TYPE</td>
    <td>Default type assigned to newly created network connections</td>
  </tr>
  <tr>
    <td>--maximum-supported-quality</td>
    <td>0</td>
    <td>MAXIMUM_SUPPORTED_QUALITY</td>
    <td>Maximum supported quality on newly created network connections</td>
  </tr>
  <tr>
    <td>--config-file</td>
    <td>/etc/upsf/policy.yml</td>
    <td>CONFIG_FILE</td>
    <td>Policy configuration file containing pre-defined SGUP, TSF instances</td>
  </tr>
  <tr>
    <td>--log-level</td>
    <td>info</td>
    <td>LOG_LEVEL</td>
    <td>Default loglevel, supported options: info, warning, error, critical, debug</td>
  </tr>
</table>

This application makes use of the <a
href="https://github.com/bisdn/upsf-client">upsf-client</a> library for 
UPSF related communication.

# Getting started and installation

Installation is based on <a
href="https://setuptools.pypa.io/en/latest/setuptools.html">Setuptools</a>.

For safe testing create and enter a virtual environment, build and install the application, e.g.:

```
sh# cd upsf-net-conn-manager
sh# python3 -m venv venv
sh# source venv/bin/activate
sh# python3 setup.py build && python3 setup.py install
sh# net-conn-manager -h
```

# Managing network connections

Please refer to BBF WT-474 Subscriber Session Steering for a general
introduction to existing network connection types. However, the following
introduction may be useful:

- A network connection may be dedicated to a single subscriber group
  (SS), or a network connection may be used by multiple subscriber
  groups in parallel (MS).
- A transport association assigned to a network connection may consist
  of multiple point-to-point (PTP) transport paths connecting an SGUP to all TSF
  instances, effectively defining a start-of-head (SOH) replication.
  If the underlying transport domain supports point-to-multipoint (MPTP)
  transport paths, the associated network connection may rely on this
  transport mptp capability.

From these orthogonal views, a total of four combinations arises:
- SS-PTP
- SS-MPTP
- MS-PTP
- MS-MPTP

Support for the MS variants exists in net-conn manager, the single shard
variants are not supported yet.
  
net-conn manager creates a mesh of point-to-point VxLAN tunnels
between all TSF and SGUP instances registered in the UPSF
database. We assume IP connectivity to exist for any arbitrary
TSF/SGUP pair. We do not make any assumption w.r.t. availability of
Quality of Service (QoS) on the underlying transport path.

Each TSF and SGUP item in the UPSF database must contain a default
endpoint. If none exists the item will be ignored during the meshing
process.  At the time of writing, the default endpoint must be of
type VxLAN, there is no support for transport path types L2vpn or 
PortVlan yet.

Changes to default endpoints assigned to either a TSF or SGUP instance will be
detected by net-conn manager, and the associated network connections will be
recalculated as needed.

## Multi-SubscriberGroup-PointToPoint (MS-PTP)

In MS-PTP mode net-conn manager creates a dedicated network connection
for each TSF/SGUP pair. Multiple subscriber groups may be mapped to such a
network connection.

## Multi-SubscriberGroup-MultiPointToPoint (MS-MPTP)

In MS-MPTP mode net-conn manager creates a multipoint network
connection between an SGUP instance and all existing TSF instances.
Multiple subscriber groups may be mapped to such a network connection.

## Single-SubscriberGroup-PointToPoint (SS-PTP)

Not supported yet.

## Single-SubscriberGroup-MultiPointToPoint (SS-MPTP)

Not supported yet.

# Policy configuration file

A configuration file is used for creating pre-defined entities in
the UPSF. A background task ensures existence of those entities,
but will not alter them unless the entity does not exist in the
UPSF yet. Existing entities with or without changes applied by other
UPSF clients will not be altered by net-conn manager. For re-creating
the original entity as defined in the policy file, you must remove
the item from the UPSF first and net-conn's background task will
recreate it after a short period of time.

Please note: this functionality is for testing only and we expect
SGUP and TSF instances to conduct their registration autonomously
in non-testing environments.

See below for an example policy configuration or inspect the examples
in the <a href="./tools/policy.yml">tools/</a> directory:

```
tsf:
  - name: tsf-A
    endpoints:
      - type: "vtep"
        params:
          ip_address: "10.10.10.100"
          udp_port: 4789
          vni: 111111
        default: "yes"
      - type: "l2vpn"
        params:
          vpn_id: 1000
      - type: "port_vlan"
        params:
          logical_port: "eth0"
          svlan: 0
          cvlan: 0
userplane:
  - name: up-A-1
    service_gateway_name: sg-A
    max_session_count: 1024
    max_shards: 16
    supported_service_group:
      - "basic-internet"
      - "something"
      - "somewhere"
    endpoints:
      - type: "vtep"
        params:
          ip_address: "10.10.10.101"
          udp_port: 4789
          vni: 111111
        default: "yes"
  - name: up-A-2
    service_gateway_name: sg-A
    max_session_count: 1024
    max_shards: 16
    supported_service_group:
      - "basic-internet"
      - "something"
      - "somewhere"
    endpoints:
      - type: "vtep"
        params:
          ip_address: "10.10.10.102"
          udp_port: 4789
          vni: 111111
        default: "yes"
```

You may specify multiple endpoints for a SGUP or TSF instance. Keyword "default"
identifies the endpoint used as default endpoint for the associated item.


# Limitations

* net-conn manager supports VxLAN based point-to-point connections
  only. Support for different network connection types (l2vpn, port_vlan)
  is still for further study. 

* net-conn manager supports network connection types MS-PTP and MS-MPTP
  only. Both SS-PTP and SS-MPTP are not supported yet. These nc types are
  for further study.
