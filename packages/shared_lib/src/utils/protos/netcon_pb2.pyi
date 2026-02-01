from typing import ClassVar as _ClassVar
from typing import Optional as _Optional
from typing import Union as _Union

from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper

DESCRIPTOR: _descriptor.FileDescriptor

class request_e(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    SERVERDATA_REQUEST_EXECCOMMAND: _ClassVar[request_e]
    SERVERDATA_REQUEST_AUTH: _ClassVar[request_e]
    SERVERDATA_REQUEST_SEND_CONSOLE_LOG: _ClassVar[request_e]

class response_e(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    SERVERDATA_RESPONSE_AUTH: _ClassVar[response_e]
    SERVERDATA_RESPONSE_CONSOLE_LOG: _ClassVar[response_e]

SERVERDATA_REQUEST_EXECCOMMAND: request_e
SERVERDATA_REQUEST_AUTH: request_e
SERVERDATA_REQUEST_SEND_CONSOLE_LOG: request_e
SERVERDATA_RESPONSE_AUTH: response_e
SERVERDATA_RESPONSE_CONSOLE_LOG: response_e

class request(_message.Message):
    __slots__ = ("messageId", "messageType", "requestType", "requestMsg", "requestVal")
    MESSAGEID_FIELD_NUMBER: _ClassVar[int]
    MESSAGETYPE_FIELD_NUMBER: _ClassVar[int]
    REQUESTTYPE_FIELD_NUMBER: _ClassVar[int]
    REQUESTMSG_FIELD_NUMBER: _ClassVar[int]
    REQUESTVAL_FIELD_NUMBER: _ClassVar[int]
    messageId: int
    messageType: int
    requestType: request_e
    requestMsg: str
    requestVal: str
    def __init__(
        self,
        messageId: _Optional[int] = ...,
        messageType: _Optional[int] = ...,
        requestType: _Optional[_Union[request_e, str]] = ...,
        requestMsg: _Optional[str] = ...,
        requestVal: _Optional[str] = ...,
    ) -> None: ...

class response(_message.Message):
    __slots__ = ("messageId", "messageType", "responseType", "responseMsg", "responseVal")
    MESSAGEID_FIELD_NUMBER: _ClassVar[int]
    MESSAGETYPE_FIELD_NUMBER: _ClassVar[int]
    RESPONSETYPE_FIELD_NUMBER: _ClassVar[int]
    RESPONSEMSG_FIELD_NUMBER: _ClassVar[int]
    RESPONSEVAL_FIELD_NUMBER: _ClassVar[int]
    messageId: int
    messageType: int
    responseType: response_e
    responseMsg: str
    responseVal: str
    def __init__(
        self,
        messageId: _Optional[int] = ...,
        messageType: _Optional[int] = ...,
        responseType: _Optional[_Union[response_e, str]] = ...,
        responseMsg: _Optional[str] = ...,
        responseVal: _Optional[str] = ...,
    ) -> None: ...

class envelope(_message.Message):
    __slots__ = ("encrypted", "nonce", "data")
    ENCRYPTED_FIELD_NUMBER: _ClassVar[int]
    NONCE_FIELD_NUMBER: _ClassVar[int]
    DATA_FIELD_NUMBER: _ClassVar[int]
    encrypted: bool
    nonce: bytes
    data: bytes
    def __init__(self, encrypted: bool = ..., nonce: _Optional[bytes] = ..., data: _Optional[bytes] = ...) -> None: ...
