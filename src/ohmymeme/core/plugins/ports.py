# pyright: basic

from typing import Protocol


class DeviceApprovalPort(Protocol):
    def approve_device(self, device): ...


class LanByteTransportPort(Protocol):
    def receive_bytes(self, size): ...

    def send_bytes(self, data): ...

    def close(self): ...


class LanSecurityPort(Protocol):
    def validate_proof(self, proof): ...

    def derive_session_key(self): ...


class LanCommandPort(Protocol):
    def dispatch_command(self, message): ...


class SecureSessionPort(Protocol):
    def encrypt_frame(self, message): ...

    def decrypt_frame(self, frame): ...


class ReplayPolicyPort(Protocol):
    def check_and_record(self, session_id, direction, sequence, command): ...
