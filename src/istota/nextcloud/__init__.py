"""Nextcloud client package: OCS control plane, WebDAV, and sharing.

Public surface is re-exported here; ``istota.nextcloud_client`` remains as a
back-compat shim over the ``None``-returning legacy variants that best-effort
daemon paths (startup hydration, the shared-file organizer, ``!search``) still
depend on.
"""

from ._http import (
    DAV_TIMEOUT,
    DEFAULT_TIMEOUT,
    OcsError,
    OcsResult,
    PathScopeError,
    dav_files_url,
    dav_request,
    dav_root_url,
    describe_ocs_status,
    is_ocs_success,
    nc_auth,
    nc_base_url,
    nc_configured,
    ocs_delete,
    ocs_get,
    ocs_headers,
    ocs_post,
    ocs_put,
    ocs_request,
    ocs_request_full,
    resolve_scoped_path,
    workspace_root,
)

__all__ = [
    "DAV_TIMEOUT",
    "DEFAULT_TIMEOUT",
    "OcsError",
    "OcsResult",
    "PathScopeError",
    "dav_files_url",
    "dav_request",
    "dav_root_url",
    "describe_ocs_status",
    "is_ocs_success",
    "nc_auth",
    "nc_base_url",
    "nc_configured",
    "ocs_delete",
    "ocs_get",
    "ocs_headers",
    "ocs_post",
    "ocs_put",
    "ocs_request",
    "ocs_request_full",
    "resolve_scoped_path",
    "workspace_root",
]
