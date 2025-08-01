import configparser
import sys

from sqlalchemy import Table, select, func, delete
from sqlalchemy.dialects.sqlite import Insert
from sqlalchemy.exc import (
    NoInspectionAvailable,
    NoSuchTableError,
)

from nxc.database import BaseDB, format_host_query
from nxc.logger import nxc_logger
from nxc.paths import CONFIG_PATH

# we can't import config.py due to a circular dependency, so we have to create redundant code unfortunately
nxc_config = configparser.ConfigParser()
nxc_config.read(CONFIG_PATH)
nxc_workspace = nxc_config.get("nxc", "workspace", fallback="default")


class database(BaseDB):
    def __init__(self, db_engine):
        self.CredentialsTable = None
        self.HostsTable = None
        self.LoggedinRelationsTable = None
        self.AdminRelationsTable = None
        self.KeysTable = None

        super().__init__(db_engine)

    @staticmethod
    def db_schema(db_conn):
        db_conn.execute(
            """CREATE TABLE "credentials" (
            "id" integer PRIMARY KEY,
            "username" text,
            "password" text,
            "credtype" text
        )"""
        )
        db_conn.execute(
            """CREATE TABLE "hosts" (
            "id" integer PRIMARY KEY,
            "host" text,
            "port" integer,
            "banner" text,
            "os" text
        )"""
        )
        db_conn.execute(
            """CREATE TABLE "loggedin_relations" (
            "id" integer PRIMARY KEY,
            "credid" integer,
            "hostid" integer,
            "shell" boolean,
            FOREIGN KEY(credid) REFERENCES credentials(id),
            FOREIGN KEY(hostid) REFERENCES hosts(id)
        )"""
        )
        # "admin" access with SSH means we have root access, which implies shell access since we run commands to check
        db_conn.execute(
            """CREATE TABLE "admin_relations" (
            "id" integer PRIMARY KEY,
            "credid" integer,
            "hostid" integer,
            FOREIGN KEY(credid) REFERENCES credentials(id),
            FOREIGN KEY(hostid) REFERENCES hosts(id)
        )"""
        )
        db_conn.execute(
            """CREATE TABLE "keys" (
            "id" integer PRIMARY KEY,
            "credid" integer,
            "data" text,
            FOREIGN KEY(credid) REFERENCES credentials(id)
        )"""
        )

    def reflect_tables(self):
        with self.db_engine.connect():
            try:
                self.CredentialsTable = Table("credentials", self.metadata, autoload_with=self.db_engine)
                self.HostsTable = Table("hosts", self.metadata, autoload_with=self.db_engine)
                self.LoggedinRelationsTable = Table("loggedin_relations", self.metadata, autoload_with=self.db_engine)
                self.AdminRelationsTable = Table("admin_relations", self.metadata, autoload_with=self.db_engine)
                self.KeysTable = Table("keys", self.metadata, autoload_with=self.db_engine)
            except (NoInspectionAvailable, NoSuchTableError):
                print(
                    f"""
                    [-] Error reflecting tables for the {self.protocol} protocol - this means there is a DB schema mismatch
                    [-] This is probably because a newer version of nxc is being run on an old DB schema
                    [-] Optionally save the old DB data (`cp {self.db_path} ~/nxc_{self.protocol.lower()}.bak`)
                    [-] Then remove the nxc {self.protocol} DB (`rm -f {self.db_path}`) and run nxc to initialize the new DB"""
                )
                sys.exit()

    def add_host(self, host, port, banner, os=None):
        """Check if this host has already been added to the database, if not, add it in."""
        hosts = []
        updated_ids = []

        q = select(self.HostsTable).filter(self.HostsTable.c.host == host)
        results = self.db_execute(q).all()
        nxc_logger.debug(f"add_host(): Initial hosts results: {results}")

        # create new host
        if not results:
            new_host = {
                "host": host,
                "port": port,
                "banner": banner if banner is not None else "",
                "os": os if os is not None else "",
            }
            hosts = [new_host]
        # update existing hosts data
        else:
            for host_result in results:
                host_data = host_result._asdict()
                nxc_logger.debug(f"host: {host_result}")
                nxc_logger.debug(f"host_data: {host_data}")
                # only update column if it is being passed in
                if host is not None:
                    host_data["host"] = host
                if port is not None:
                    host_data["port"] = port
                if banner is not None:
                    host_data["banner"] = banner
                if os is not None:
                    host_data["os"] = os
                # only add host to be updated if it has changed
                if host_data not in hosts:
                    hosts.append(host_data)
                    updated_ids.append(host_data["id"])
        nxc_logger.debug(f"Hosts: {hosts}")

        # TODO: find a way to abstract this away to a single Upsert call
        q = Insert(self.HostsTable)  # .returning(self.HostsTable.c.id)
        update_columns = {col.name: col for col in q.excluded if col.name not in "id"}
        q = q.on_conflict_do_update(index_elements=self.HostsTable.primary_key, set_=update_columns)

        self.db_execute(q, hosts)  # .scalar()
        # we only return updated IDs for now - when RETURNING clause is allowed we can return inserted
        if updated_ids:
            nxc_logger.debug(f"add_host() - Host IDs Updated: {updated_ids}")
            return updated_ids

    def add_credential(self, credtype, username, password, key=None):
        """Check if this credential has already been added to the database, if not add it in."""
        credentials = []

        # a user can have multiple keys, all with passphrases, and a separate login password
        if key is not None:
            q = (
                select(self.CredentialsTable)
                .join(self.KeysTable)
                .filter(
                    func.lower(self.CredentialsTable.c.username) == func.lower(username),
                    func.lower(self.CredentialsTable.c.credtype) == func.lower(credtype),
                    self.KeysTable.c.data == key,
                )
            )
            results = self.db_execute(q).all()
        else:
            q = select(self.CredentialsTable).filter(
                func.lower(self.CredentialsTable.c.username) == func.lower(username),
                func.lower(self.CredentialsTable.c.credtype) == func.lower(credtype),
            )
            results = self.db_execute(q).all()

        # add new credential
        if not results:
            new_cred = {
                "credtype": credtype,
                "username": username,
                "password": password,
            }
            credentials = [new_cred]
        # update existing cred data
        else:
            for creds in results:
                # this will include the id, so we don't touch it
                cred_data = creds._asdict()
                # only update column if it is being passed in
                if credtype is not None:
                    cred_data["credtype"] = credtype
                if username is not None:
                    cred_data["username"] = username
                if password is not None:
                    cred_data["password"] = password
                # only add cred to be updated if it has changed
                if cred_data not in credentials:
                    credentials.append(cred_data)

        # TODO: find a way to abstract this away to a single Upsert call
        q_users = Insert(self.CredentialsTable)  # .returning(self.CredentialsTable.c.id)
        update_columns_users = {col.name: col for col in q_users.excluded if col.name not in "id"}
        q_users = q_users.on_conflict_do_update(index_elements=self.CredentialsTable.primary_key,
                                                set_=update_columns_users)
        nxc_logger.debug(f"Adding credentials: {credentials}")

        self.db_execute(q_users, credentials)  # .scalar()

        # hacky way to get cred_id since we can't use returning() yet
        if len(credentials) == 1:
            cred_id = self.get_credential(credtype, username, password)
            if key is not None:
                self.add_key(cred_id, key)
            return cred_id
        else:
            return credentials

    def remove_credentials(self, creds_id):
        """Removes a credential ID from the database"""
        del_hosts = []
        for cred_id in creds_id:
            q = delete(self.CredentialsTable).filter(self.CredentialsTable.c.id == cred_id)
            del_hosts.append(q)
        self.db_execute(q)

    def add_key(self, cred_id, key):
        # check if key relation already exists
        check_q = self.db_execute(select(self.KeysTable).filter(self.KeysTable.c.credid == cred_id)).all()
        nxc_logger.debug(f"check_q: {check_q}")
        if check_q:
            nxc_logger.debug(f"Key already exists for cred_id {cred_id}")
            return None

        key_data = {"credid": cred_id, "data": key}
        self.db_execute(Insert(self.KeysTable), key_data)
        key_id = self.db_execute(select(self.KeysTable).filter(self.KeysTable.c.credid == cred_id)).all()[0].id
        nxc_logger.debug(f"Key added: {key_id}")
        return key_id

    def get_keys(self, key_id=None, cred_id=None):
        q = select(self.KeysTable)
        if key_id is not None:
            q = q.filter(self.KeysTable.c.id == key_id)
        elif cred_id is not None:
            q = q.filter(self.KeysTable.c.credid == cred_id)
        return self.db_execute(q).all()

    def add_admin_user(self, credtype, username, secret, host_id=None, cred_id=None):
        add_links = []

        creds_q = select(self.CredentialsTable)
        if cred_id:  # noqa: SIM108
            creds_q = creds_q.filter(self.CredentialsTable.c.id == cred_id)
        else:
            creds_q = creds_q.filter(
                func.lower(self.CredentialsTable.c.credtype) == func.lower(credtype),
                func.lower(self.CredentialsTable.c.username) == func.lower(username),
                self.CredentialsTable.c.password == secret,
            )
        creds = self.db_execute(creds_q)
        hosts = self.get_hosts(host_id)

        if creds and hosts:
            for cred, host in zip(creds, hosts, strict=True):
                cred_id = cred[0]
                host_id = host[0]
                link = {"credid": cred_id, "hostid": host_id}
                admin_relations_select = select(self.AdminRelationsTable).filter(
                    self.AdminRelationsTable.c.credid == cred_id,
                    self.AdminRelationsTable.c.hostid == host_id,
                )
                links = self.db_execute(admin_relations_select).all()

                if not links:
                    add_links.append(link)

        admin_relations_insert = Insert(self.AdminRelationsTable)

        if add_links:
            self.db_execute(admin_relations_insert, add_links)

    def get_admin_relations(self, cred_id=None, host_id=None):
        if cred_id:
            q = select(self.AdminRelationsTable).filter(self.AdminRelationsTable.c.credid == cred_id)
        elif host_id:
            q = select(self.AdminRelationsTable).filter(self.AdminRelationsTable.c.hostid == host_id)
        else:
            q = select(self.AdminRelationsTable)

        return self.db_execute(q).all()

    def remove_admin_relation(self, cred_ids=None, host_ids=None):
        q = delete(self.AdminRelationsTable)
        if cred_ids:
            for cred_id in cred_ids:
                q = q.filter(self.AdminRelationsTable.c.credid == cred_id)
        elif host_ids:
            for host_id in host_ids:
                q = q.filter(self.AdminRelationsTable.c.hostid == host_id)
        self.db_execute(q)

    def is_credential_valid(self, credential_id):
        """Check if this credential ID is valid."""
        q = select(self.CredentialsTable).filter(
            self.CredentialsTable.c.id == credential_id,
            self.CredentialsTable.c.password is not None,
        )
        results = self.db_execute(q).all()
        return len(results) > 0

    def get_credentials(self, filter_term=None, cred_type=None):
        """Return credentials from the database."""
        # if we're returning a single credential by ID
        if self.is_credential_valid(filter_term):
            q = select(self.CredentialsTable).filter(self.CredentialsTable.c.id == filter_term)
        elif cred_type:
            q = select(self.CredentialsTable).filter(self.CredentialsTable.c.credtype == cred_type)
        # if we're filtering by username
        elif filter_term and filter_term != "":
            like_term = func.lower(f"%{filter_term}%")
            q = select(self.CredentialsTable).filter(func.lower(self.CredentialsTable.c.username).like(like_term))
        # otherwise return all credentials
        else:
            q = select(self.CredentialsTable)

        return self.db_execute(q).all()

    def get_credential(self, cred_type, username, password):
        q = select(self.CredentialsTable).filter(
            self.CredentialsTable.c.username == username,
            self.CredentialsTable.c.password == password,
            self.CredentialsTable.c.credtype == cred_type,
        )
        results = self.db_execute(q).first()
        if results is not None:
            return results.id

    def is_host_valid(self, host_id):
        """Check if this host ID is valid."""
        q = select(self.HostsTable).filter(self.HostsTable.c.id == host_id)
        results = self.db_execute(q).all()
        return len(results) > 0

    def get_hosts(self, filter_term=None):
        """Return hosts from the database."""
        q = select(self.HostsTable)

        # if we're returning a single host by ID
        if self.is_host_valid(filter_term):
            q = q.filter(self.HostsTable.c.id == filter_term)
            results = self.db_execute(q).first()
            # all() returns a list, so we keep the return format the same so consumers don't have to guess
            return [results]
        # if we're filtering by host
        elif filter_term and filter_term != "":
            q = format_host_query(q, filter_term, self.HostsTable)

        results = self.db_execute(q).all()
        nxc_logger.debug(f"SSH get_hosts() - results: {results}")
        return results

    def is_user_valid(self, cred_id):
        """Check if this User ID is valid."""
        q = select(self.CredentialsTable).filter(self.CredentialsTable.c.id == cred_id)
        results = self.db_execute(q).all()
        return len(results) > 0

    def get_users(self, filter_term=None):
        q = select(self.CredentialsTable)

        if self.is_user_valid(filter_term):
            q = q.filter(self.CredentialsTable.c.id == filter_term)
        # if we're filtering by username
        elif filter_term and filter_term != "":
            like_term = func.lower(f"%{filter_term}%")
            q = q.filter(func.lower(self.CredentialsTable.c.username).like(like_term))
        return self.db_execute(q).all()

    def get_user(self, domain, username):
        q = select(self.CredentialsTable).filter(func.lower(self.CredentialsTable.c.username) == func.lower(username))
        return self.db_execute(q).all()

    def add_loggedin_relation(self, cred_id, host_id, shell=False):
        relation_query = select(self.LoggedinRelationsTable).filter(
            self.LoggedinRelationsTable.c.credid == cred_id,
            self.LoggedinRelationsTable.c.hostid == host_id,
        )
        results = self.db_execute(relation_query).all()

        # only add one if one doesn't already exist
        if not results:
            relation = {"credid": cred_id, "hostid": host_id, "shell": shell}
            try:
                nxc_logger.debug(f"Inserting loggedin_relations: {relation}")
                # TODO: find a way to abstract this away to a single Upsert call
                q = Insert(self.LoggedinRelationsTable)  # .returning(self.LoggedinRelationsTable.c.id)

                self.db_execute(q, [relation])  # .scalar()
                inserted_id_results = self.get_loggedin_relations(cred_id, host_id)
                nxc_logger.debug(f"Checking if relation was added: {inserted_id_results}")
                return inserted_id_results[0].id
            except Exception as e:
                nxc_logger.debug(f"Error inserting LoggedinRelation: {e}")

    def get_loggedin_relations(self, cred_id=None, host_id=None, shell=None):
        q = select(self.LoggedinRelationsTable)  # .returning(self.LoggedinRelationsTable.c.id)
        if cred_id:
            q = q.filter(self.LoggedinRelationsTable.c.credid == cred_id)
        if host_id:
            q = q.filter(self.LoggedinRelationsTable.c.hostid == host_id)
        if shell:
            q = q.filter(self.LoggedinRelationsTable.c.shell == shell)
        return self.db_execute(q).all()

    def remove_loggedin_relations(self, cred_id=None, host_id=None):
        q = delete(self.LoggedinRelationsTable)
        if cred_id:
            q = q.filter(self.LoggedinRelationsTable.c.credid == cred_id)
        elif host_id:
            q = q.filter(self.LoggedinRelationsTable.c.hostid == host_id)
        self.db_execute(q)
