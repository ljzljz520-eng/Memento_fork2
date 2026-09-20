"""Egress policy for cloud-facing surfaces (embeddings and chat).

Independent from the capture decision: the capture policy decides what is
recorded on disk, the egress policy decides which metadata fields may leave
the machine when texts are embedded or sent to the chat LLM. Only fields on
the allowlist are ever forwarded.
"""

DEFAULT_ALLOWED_FIELDS = frozenset(["id", "time"])


class EgressPolicy:
    def __init__(self, allowed_fields=None):
        if allowed_fields is None:
            self.allowed_fields = frozenset(DEFAULT_ALLOWED_FIELDS)
        else:
            self.allowed_fields = frozenset(allowed_fields)

    @classmethod
    def from_config(cls, config):
        allowed = None
        if isinstance(config, dict):
            egress = config.get("egress")
            if isinstance(egress, dict):
                allowed = egress.get("allowed_fields")
        return cls(allowed)

    def permits(self, field):
        return field in self.allowed_fields

    def filter_metadata(self, metadata):
        if not isinstance(metadata, dict):
            return {}
        return {k: v for k, v in metadata.items() if k in self.allowed_fields}

    def filter_metadatas(self, metadatas):
        return [self.filter_metadata(md) for md in metadatas]
