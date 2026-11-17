"""In-memory keyring backend so tests never touch the real OS keyring."""

from keyring.backend import KeyringBackend


class MemoryKeyring(KeyringBackend):
    priority = 1
    store: dict = {}

    def get_password(self, service, username):
        return self.store.get((service, username))

    def set_password(self, service, username, password):
        self.store[(service, username)] = password

    def delete_password(self, service, username):
        self.store.pop((service, username), None)
