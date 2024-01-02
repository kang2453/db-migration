import logging
from conf import DEFAULT_LOGGER
from lib import MongoCustomClient
from lib.util import print_log
from datetime import datetime
from spaceone.core.utils import generate_id

_LOGGER = logging.getLogger(DEFAULT_LOGGER)

WORKSPACE_MAP = {
    "single": {
        # {domain_id} : {workspace_id}
    },
    "multi": {
        #     {domain_id} : {
        #         {project_group_id}: {workspace_id : workspace_id, project_ids : []},
        #         {project_group_id}: {workspace_id}
        #     }
    },
}

PROJECT_MAP = {
    # {domain_id} : {
    #     {project_id} : {workspace_id}
    #     {project_id} : {workspace_id}
    #     {project_id} : {workspace_id}
    # }
}


@print_log
def drop_collections(mongo_client, collections):
    for collection in collections:
        mongo_client.drop_collection("IDENTITY", collection)


@print_log
def identity_domain_refactoring_and_external_auth_creating(
    mongo_client: MongoCustomClient, domain_id_param
):
    domains = mongo_client.find(
        "IDENTITY", "domain", {"domain_id": domain_id_param}, {}
    )

    for domain in domains:
        domain_id = domain["domain_id"]
        domain_state = domain["state"]
        created_at = domain["created_at"]

        plugin_info = domain.get("plugin_info", {})
        tags = domain.get("tags")

        if workspace_mode := tags.get("workspace_mode"):
            if workspace_mode == "multi":
                WORKSPACE_MAP["multi"].update({domain_id: {}})
            else:
                WORKSPACE_MAP["single"].update({domain_id: ""})

        if plugin_info and domain_state != "DELETED":
            params = {
                "domain_id": domain_id,
                "state": domain_state,
                "plugin_info": plugin_info,
                "updated_at": created_at,
            }
            mongo_client.insert_one("IDENTITY", "external_auth", params, is_new=True)

        if "config" in domain.keys() and "plugin_info" in domain.keys():
            query = {"domain_id": domain_id}
            update_params = {"$unset": {"plugin_info": 1, "config": 1, "deleted_at": 1}}
            mongo_client.update_one("IDENTITY", "domain", query, update_params)


@print_log
def identity_project_group_refactoring_and_workspace_creating(
    mongo_client: MongoCustomClient, domain_id_param
):
    project_groups = mongo_client.find(
        "IDENTITY", "project_group", {"domain_id": domain_id_param}, {}
    )
    for project_group in project_groups:
        if "parent_project_group" in project_group.keys():
            domain_id = project_group["domain_id"]
            project_group_id = project_group["project_group_id"]
            parent_project_group_id = project_group.get("parent_project_group_id")
            project_group_name = project_group["name"]

            unset_params = {"$unset": {"parent_project_group": 1, "created_by": 1}}

            set_params = {"$set": {}}

            if domain_id in WORKSPACE_MAP["multi"].keys():
                if not parent_project_group_id:
                    _create_workspace(domain_id, mongo_client, project_group_name)
                    workspace_id = _get_workspace_id(
                        domain_id, mongo_client, project_group_name
                    )
                    WORKSPACE_MAP["multi"][domain_id].update(
                        {project_group_id: workspace_id}
                    )
                    set_params["$set"].update({"workspace_id": workspace_id})
                else:
                    root_project_group = _get_root_project_group_id_by_project_group_id(
                        domain_id, parent_project_group_id, mongo_client
                    )
                    root_project_group_id = root_project_group["project_group_id"]
                    root_project_group_name = root_project_group["name"]

                    if (
                        root_project_group_id
                        in WORKSPACE_MAP["multi"][domain_id].keys()
                    ):
                        workspace_id = WORKSPACE_MAP["multi"][domain_id][
                            root_project_group_id
                        ]
                        set_params["$set"].update({"workspace_id": workspace_id})
                    else:
                        _create_workspace(
                            domain_id, mongo_client, root_project_group_name
                        )
                        workspace_id = _get_workspace_id(
                            domain_id, mongo_client, root_project_group_name
                        )
                        WORKSPACE_MAP["multi"][domain_id].update(
                            {root_project_group_id: workspace_id}
                        )
                        set_params["$set"].update({"workspace_id": workspace_id})
            else:
                workspace_id = WORKSPACE_MAP["single"].get(domain_id)
                if not workspace_id:
                    _create_workspace(domain_id, mongo_client)
                    workspace_id = _get_workspace_id(domain_id, mongo_client)
                    WORKSPACE_MAP["single"][domain_id] = workspace_id
                set_params["$set"].update({"workspace_id": workspace_id})

            mongo_client.update_one(
                "IDENTITY", "project_group", {"_id": project_group["_id"]}, set_params
            )
            mongo_client.update_one(
                "IDENTITY", "project_group", {"_id": project_group["_id"]}, unset_params
            )


@print_log
def identity_project_refactoring(mongo_client: MongoCustomClient, domain_id_param):
    projects = mongo_client.find(
        "IDENTITY", "project", {"domain_id": domain_id_param}, {}
    )

    # if projects does not exist.
    if not projects:
        _LOGGER.error(f"domain({domain_id_param}) has no projects.")
        return

    for project in projects:
        if "project_group" in project.keys():
            # unset_params = {"$unset": {"project_group": 1}}

            set_params = {
                "$set": {
                    "project_type": "PRIVATE",
                }
            }

            project_id = project["project_id"]
            domain_id = project["domain_id"]
            project_group_id = project.get("project_group_id")
            root_project_group_id = _get_root_project_group_id_by_project_group_id(
                domain_id, project_group_id, mongo_client
            )["project_group_id"]
            workspace_id = ""

            if domain_id in WORKSPACE_MAP["multi"].keys():
                if root_project_group_id in WORKSPACE_MAP["multi"][domain_id].keys():
                    workspace_id = WORKSPACE_MAP["multi"][domain_id][
                        root_project_group_id
                    ]

            if domain_id in WORKSPACE_MAP["single"].keys():
                workspace_id = WORKSPACE_MAP["single"][domain_id]

            if not workspace_id:
                _LOGGER.error(
                    f"Project({project_id}) has no workspace_id. (project: {project})"
                )

            if domain_id not in PROJECT_MAP.keys():
                PROJECT_MAP[domain_id] = {project_id: workspace_id}
            else:
                PROJECT_MAP[domain_id].update({project_id: workspace_id})

            users = []
            if pg_role_bindings := mongo_client.find(
                "IDENTITY", "role_binding", {"project_group_id": project_group_id}, {}
            ):
                for role_binding in pg_role_bindings:
                    if (
                        role_binding["resource_type"] == "identity.User"
                        and role_binding["resource_id"] not in users
                    ):
                        users.append(role_binding["resource_id"])

            if project_role_bindings := mongo_client.find(
                "IDENTITY", "role_binding", {"project_id": project_id}, {}
            ):
                for role_binding in project_role_bindings:
                    if (
                        role_binding["resource_type"] == "identity.User"
                        and role_binding["resource_id"] not in users
                    ):
                        users.append(role_binding["resource_id"])

            set_params["$set"].update({"workspace_id": workspace_id, "users": users})

            mongo_client.update_one(
                "IDENTITY", "project", {"_id": project["_id"]}, set_params
            )


def _create_workspace(domain_id, mongo_client, project_group_name=None):
    workspaces = mongo_client.find(
        "IDENTITY", "workspace", {"domain_id": domain_id}, {}
    )
    workspace_ids = [workspace["workspace_id"] for workspace in workspaces]

    create_params = {
        "name": "Default",
        "state": "ENABLED",
        "tags": {},
        "domain_id": domain_id,
        "created_by": "SpaceONE",
        "created_at": datetime.utcnow(),
        "deleted_at": None,
    }

    workspace_id = generate_id("workspace")
    if workspace_id not in workspace_ids:
        create_params.update({"workspace_id": workspace_id})

    if project_group_name:
        create_params["name"] = project_group_name

    mongo_client.insert_one("IDENTITY", "workspace", create_params, is_new=True)


def _get_workspace_id(domain_id, mongo_client, workspace_name=None):
    query = {"domain_id": domain_id}

    if workspace_name:
        query.update({"name": workspace_name})

    workspaces = mongo_client.find("IDENTITY", "workspace", query, {})
    return [workspace["workspace_id"] for workspace in workspaces][0]


def _get_root_project_group_id_by_project_group_id(
    domain_id, project_group_id, mongo_client
):
    while project_group_id:
        project_group = mongo_client.find_one(
            "IDENTITY",
            "project_group",
            {"project_group_id": project_group_id, "domain_id": domain_id},
            {},
        )
        if parent_project_group_id := project_group.get("parent_project_group_id"):
            project_group_id = parent_project_group_id

        else:
            return project_group


@print_log
def identity_service_account_and_trusted_account_creating(
    mongo_client, domain_id_param
):
    # run trusted account first
    service_account_infos = mongo_client.find(
        "IDENTITY",
        "service_account",
        {"domain_id": domain_id_param, "service_account_type": "TRUSTED"},
        {},
    )
    domain_id = ""

    # create trusted_account using service_account_type and remove service_account_type
    for service_account_info in service_account_infos:
        domain_id = service_account_info["domain_id"]
        # service_account_type = service_account_info["service_account_type"]
        # project_id = service_account_info.get('project_info', {}).get('project_id')
        # workspace_id = PROJECT_MAP[domain_id].get(project_id)
        # create trusted
        trusted_account_id = generate_id("ta")

        # ref with trusted_secret. get trusted_secret using sa id.
        # create new trusted_account then change secret_account_id to trusted_account_id in trusted_secret.
        trusted_secret = mongo_client.find_one(
            "SECRET",
            "trusted_secret",
            {
                "domain_id": domain_id,
                "service_account_id": service_account_info["service_account_id"],
            },
            {},
        )

        trusted_account_create = {
            "trusted_account_id": trusted_account_id,
            "name": service_account_info.get("name"),
            "data": service_account_info.get("data"),
            "provider": service_account_info.get("provider"),
            "tags": service_account_info.get("tags"),
            "secret_schema_id": trusted_secret.get("schema", ""),
            "trusted_secret_id": trusted_secret.get("trusted_secret_id"),
            "resource_group": "DOMAIN",
            "workspace_id": "*",
            "domain_id": service_account_info["domain_id"],
            "created_at": datetime.utcnow(),
        }

        mongo_client.insert_one(
            "IDENTITY", "trusted_account", trusted_account_create, is_new=True
        )

        # update trusted_service_account_id that has sa-account_id
        mongo_client.update_many(
            "IDENTITY",
            "service_account",
            {"trusted_service_account_id": service_account_info["service_account_id"]},
            {"$set": {"trusted_service_account_id": trusted_account_id}},
        )

        # remove this
        mongo_client.delete_many(
            "IDENTITY", "service_account", {"_id": service_account_info["_id"]}
        )

        # add trusted_account_id in trusted_secret
        mongo_client.update_one(
            "SECRET",
            "trusted_secret",
            {"_id": trusted_secret["_id"]},
            {"$set": {"trusted_account_id": trusted_account_id}},
        )

        # remove service_account_id in trusted_secret
        mongo_client.update_one(
            "SECRET",
            "trusted_secret",
            {"_id": trusted_secret["_id"]},
            {"$unset": {"secret_account_id": 1}},
        )

    # non trusted type
    service_account_infos = mongo_client.find(
        "IDENTITY", "service_account", {"domain_id": domain_id_param}, {}
    )
    for service_account_info in service_account_infos:
        # service_account_type == GENERAL
        # if there is no project_id, create new project first workspace named by unmanaged-sa-project.
        # then move this project
        # first_workspace : print(list(PROJECT_MAP['domain-abc'][0].values())[0])

        # project_id = ""
        # workspace_id = ""

        """check project_id"""
        if service_account_info.get("project_id"):
            # has project_id
            project_id = service_account_info.get("project_id")
            workspace_id = PROJECT_MAP[domain_id].get(project_id)
        else:
            # has no project_id
            if service_account_info.get("project"):
                # get project_id from project_info
                project_info = service_account_info.get("project")
                project_id = project_info("project_id")
                workspace_id = PROJECT_MAP[domain_id].get(project_id)
            else:
                # has no project too, create new project at first workspace
                workspace_id = list(PROJECT_MAP[domain_id][0].values())[0]
                project_id = create_unmanaged_sa_project(
                    domain_id, workspace_id, mongo_client
                )

        if not project_id or not workspace_id:
            _LOGGER.error(
                f"Project({project_id}) has no workspace_id. (domain_id: {domain_id})"
            )

        set_param = {
            "$set": {"project_id": project_id, "workspace_id": workspace_id},
            "$unset": {"service_account_type": 1, "project": 1, "scope": 1},
        }

        mongo_client.update_one(
            "IDENTITY",
            "service_account",
            {"_id": service_account_info["_id"]},
            set_param,
        )


@print_log
def create_unmanaged_sa_project(domain_id, workspace_id, mongo_client):
    project_id = generate_id("project")
    # create new project
    name = "unmanaged-sa-project"

    create_project_param = {
        "project_id": project_id,
        "name": name,
        "project_type": "PUBLIC",
        "created_by": "spaceone",
        "workspace_id": workspace_id,
        "domain_id": domain_id,
        "created_at": datetime.utcnow(),
    }
    mongo_client.insert_one("IDENTITY", "project", create_project_param, is_new=True)
    return project_id


@print_log
def identity_role_binding_refactoring(mongo_client, domain_id_param):
    # role_id                    | role type
    # "managed-domain-admin"     | "DOMAIN_ADMIN"
    # "managed-workspace-owner"  | "WORKSPACE_OWNER"
    # "managed-workspace-member" | "WORKSPACE_MEMBER"
    if not PROJECT_MAP.get(domain_id_param):
        _LOGGER.error(f"domain({domain_id_param}) has no projects.")
        return None

    role_binding_infos = mongo_client.find(
        "IDENTITY", "role_binding", {"domain_id": domain_id_param}, {}
    )

    for role_binding_info in role_binding_infos:
        # role_id = ""
        # role_type = ""
        # workspace_id = ""
        # resource_group = ""
        param_role_id = role_binding_info["role_id"]

        # get role
        role_info = mongo_client.find_one(
            "IDENTITY", "role", {"role_id": param_role_id}, {}
        )

        if role_info and role_info.get("role_type", "") == "DOMAIN":
            role_id = "managed-domain-admin"
            role_type = "DOMAIN_ADMIN"
            workspace_id = "*"
            resource_group = "DOMAIN"
        else:
            resource_group = "WORKSPACE"
            # check project_group info. if project_group not exists, WORKSPACE_MEMBER
            if not role_binding_info.get("project_group_id"):
                role_id = "managed-workspace-member"
                role_type = "WORKSPACE_MEMBER"
                workspace_id = PROJECT_MAP[domain_id_param].get(
                    role_binding_info.get("project_id")
                )
            else:
                # find project_group
                # if there is no parent project group_id(it means root project group), it is workspace_owner
                project_group_info = mongo_client.find_one(
                    "IDENTITY",
                    "project_group",
                    {
                        "project_group_id": role_binding_info.get("project_group_id"),
                        "parent_group_id": {"$eq": None},
                    },
                    {},
                )
                workspace_id = project_group_info.get("workspace_id")
                if project_group_info:
                    role_id = "managed-workspace-owner"
                    role_type = "WORKSPACE_OWNER"
                else:
                    role_id = "managed-workspace-member"
                    role_type = "WORKSPACE_MEMBER"

        # change resource_id to user_id
        set_param = {
            "$set": {
                "user_id": role_binding_info["resource_id"],
                "role_id": role_id,
                "role_type": role_type,
                "workspace_id": workspace_id,
                "resource_group": resource_group,
            },
            "$unset": {
                "resource_type": 1,
                "resource_id": 1,
                "role": 1,
                "project": 1,
                "project_group": 1,
                "project_id": 1,
                "project_group_id": 1,
                "user": 1,
                "labels": 1,
                "tags": 1,
            },
        }

        mongo_client.update_one(
            "IDENTITY", "role_binding", {"_id": role_binding_info["_id"]}, set_param
        )


@print_log
def identity_user_refactoring(mongo_client, domain_id_param):
    user_infos = mongo_client.find(
        "IDENTITY", "user", {"domain_id": domain_id_param}, {}
    )
    role_type = "USER"
    for user_info in user_infos:
        role_binding_info = mongo_client.find_one(
            "IDENTITY",
            "role_binding",
            {
                "domain_id": user_info["domain_id"],
                "user_id": user_info["user_id"],
                "role_type": "DOMAIN_ADMIN",
            },
            {},
        )
        if role_binding_info:
            role_type = "DOMAIN_ADMIN"

        set_param = {
            "$set": {"auth_type": user_info["backend"], "role_type": role_type},
            "$unset": {"user_type": 1, "backend": 1},
        }

        mongo_client.update_one(
            "IDENTITY", "user", {"_id": user_info["_id"]}, set_param
        )


def drop_collections(mongo_client):
    # drop role after refactoring role_binding
    collections = ["role", "domain_owner", "policy", "provider", "a_p_i_key"]
    for collection in collections:
        mongo_client.drop_collection('IDENTITY', collection)


def update_domain(mongo_client, domain_id_param, domain_tags):
    set_param = {'$set':{}}
    tags = domain_tags
    tags.update({'complate_migration':True})
    set_param['$set'].update({'tags': tags})
    mongo_client.update_one('IDENTITY', 'domain', {'domain_id':domain_id_param}, set_param)


def main(mongo_client, domain_id):
    # domain, external_auth
    identity_domain_refactoring_and_external_auth_creating(mongo_client, domain_id)

    # workspace, project_group
    identity_project_group_refactoring_and_workspace_creating(mongo_client, domain_id)
    print(WORKSPACE_MAP)
    # project
    identity_project_refactoring(mongo_client, domain_id)
    print(">>>", PROJECT_MAP)

    # service_account, trusted_account
    identity_service_account_and_trusted_account_creating(mongo_client, domain_id)

    # role_binding for user
    identity_role_binding_refactoring(mongo_client, domain_id)

    # user
    identity_user_refactoring(mongo_client, domain_id)

    return WORKSPACE_MAP, PROJECT_MAP