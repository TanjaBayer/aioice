import asyncio
import hashlib
import logging
import struct

from . import exceptions, stun
from .utils import random_transaction_id

logger = logging.getLogger("turn")

TCP_TRANSPORT = 0x06000000
UDP_TRANSPORT = 0x11000000


def is_channel_data(data):
    return (data[0] & 0xC0) == 0x40


def make_integrity_key(username, realm, password):
    return hashlib.md5(":".join([username, realm, password]).encode("utf8")).digest()


class TurnStreamMixin:
    def data_received(self, data):
        if not hasattr(self, "buffer"):
            self.buffer = b""
        self.buffer += data

        while len(self.buffer) >= 4:
            _, length = struct.unpack("!HH", self.buffer[0:4])
            if is_channel_data(self.buffer):
                full_length = 4 + length
            else:
                full_length = 20 + length
            if len(self.buffer) < full_length:
                break

            addr = self.transport.get_extra_info("peername")
            self.datagram_received(self.buffer[0:full_length], addr)
            self.buffer = self.buffer[full_length:]


class TurnClientMixin:
    def __init__(self, server, username, password, lifetime):
        self.channel_to_peer = {}
        self.peer_to_channel = {}

        self.channel_number = 0x4000
        self.integrity_key = None
        self.lifetime = lifetime
        self.nonce = None
        self.password = password
        self.receiver = None
        self.realm = None
        self.refresh_handle = None
        self.relayed_address = None
        self.server = server
        self.transactions = {}
        self.username = username

    async def channel_bind(self, channel_number, addr):
        request = stun.Message(
            message_method=stun.Method.CHANNEL_BIND, message_class=stun.Class.REQUEST
        )
        request.attributes["CHANNEL-NUMBER"] = channel_number
        request.attributes["XOR-PEER-ADDRESS"] = addr
        await self.request(request)
        logger.info("TURN channel bound %d %s", channel_number, addr)

    async def connect(self, retransmissions=None):
        """
        Create a TURN allocation.
        """
        request = stun.Message(
            message_method=stun.Method.ALLOCATE, message_class=stun.Class.REQUEST
        )
        request.attributes["LIFETIME"] = self.lifetime
        request.attributes["REQUESTED-TRANSPORT"] = UDP_TRANSPORT

        try:
            response, _ = await self.request(request, retransmissions)
        except exceptions.TransactionFailed as e:
            response = e.response
            if response.attributes["ERROR-CODE"][0] == 401:
                # update long-term credentials
                self.nonce = response.attributes["NONCE"]
                self.realm = response.attributes["REALM"]
                self.integrity_key = make_integrity_key(
                    self.username, self.realm, self.password
                )

                # retry request with authentication
                request.transaction_id = random_transaction_id()
                response, _ = await self.request(request, retransmissions)

        self.relayed_address = response.attributes["XOR-RELAYED-ADDRESS"]
        logger.info("TURN allocation created %s", self.relayed_address)

        # periodically refresh allocation
        self.refresh_handle = asyncio.ensure_future(self.refresh())

        return self.relayed_address

    def connection_made(self, transport):
        logger.debug("%s connection_made(%s)", self, transport)
        self.transport = transport

    def datagram_received(self, data, addr):
        # demultiplex channel data
        if len(data) >= 4 and is_channel_data(data):
            channel, length = struct.unpack("!HH", data[0:4])

            if len(data) >= length + 4 and self.receiver:
                peer_address = self.channel_to_peer.get(channel)
                if peer_address:
                    payload = data[4 : 4 + length]
                    self.receiver.datagram_received(payload, peer_address)

            return

        try:
            message = stun.parse_message(data)
            logger.debug("%s < %s %s", self, addr, message)
        except ValueError:
            return

        if (
            message.message_class == stun.Class.RESPONSE
            or message.message_class == stun.Class.ERROR
        ) and message.transaction_id in self.transactions:
            transaction = self.transactions[message.transaction_id]
            transaction.response_received(message, addr)

    async def delete(self):
        """
        Delete the TURN allocation.
        """
        if self.refresh_handle:
            self.refresh_handle.cancel()
            self.refresh_handle = None

        request = stun.Message(
            message_method=stun.Method.REFRESH, message_class=stun.Class.REQUEST
        )
        request.attributes["LIFETIME"] = 0
        await self.request(request)

        logger.info("TURN allocation deleted %s", self.relayed_address)
        if self.receiver:
            self.receiver.connection_lost(None)

    async def refresh(self):
        """
        Periodically refresh the TURN allocation.
        """
        while True:
            await asyncio.sleep(5 / 6 * self.lifetime)

            request = stun.Message(
                message_method=stun.Method.REFRESH, message_class=stun.Class.REQUEST
            )
            request.attributes["LIFETIME"] = self.lifetime
            await self.request(request)

            logger.info("TURN allocation refreshed %s", self.relayed_address)

    async def request(self, request, retransmissions=None):
        """
        Execute a STUN transaction and return the response.
        """
        assert request.transaction_id not in self.transactions

        if self.integrity_key:
            self.__add_authentication(request)

        transaction = stun.Transaction(request, self.server, self, retransmissions)
        self.transactions[request.transaction_id] = transaction
        try:
            return await transaction.run()
        finally:
            del self.transactions[request.transaction_id]

    async def send_data(self, data, addr):
        """
        Send data to a remote host via the TURN server.
        """
        channel = self.peer_to_channel.get(addr)
        if channel is None:
            channel = self.channel_number
            self.channel_number += 1
            self.channel_to_peer[channel] = addr
            self.peer_to_channel[addr] = channel

            # bind channel
            await self.channel_bind(channel, addr)

        header = struct.pack("!HH", channel, len(data))
        self._send(header + data)

    def send_stun(self, message, addr):
        """
        Send a STUN message to the TURN server.
        """
        logger.debug("%s > %s %s", self, addr, message)
        self._send(bytes(message))

    def __add_authentication(self, request):
        request.attributes["USERNAME"] = self.username
        request.attributes["NONCE"] = self.nonce
        request.attributes["REALM"] = self.realm
        request.add_message_integrity(self.integrity_key)
        request.add_fingerprint()


class TurnClientTcpProtocol(TurnClientMixin, TurnStreamMixin, asyncio.Protocol):
    """
    Protocol for handling TURN over TCP.
    """

    def _send(self, data):
        self.transport.write(data)

    def __repr__(self):
        return "turn/tcp"


class TurnClientUdpProtocol(TurnClientMixin, asyncio.DatagramProtocol):
    """
    Protocol for handling TURN over UDP.
    """

    def _send(self, data):
        self.transport.sendto(data)

    def __repr__(self):
        return "turn/udp"


class TurnTransport:
    """
    Behaves like a Datagram transport, but uses a TURN allocation.
    """

    def __init__(self, protocol, inner_protocol):
        self.protocol = protocol
        self.__inner_protocol = inner_protocol
        self.__inner_protocol.receiver = protocol
        self.__relayed_address = None

    def close(self):
        """
        Close the transport.

        After the TURN allocation has been deleted, the protocol's
        `connection_lost()` method will be called with None as its argument.
        """
        asyncio.ensure_future(self.__inner_protocol.delete())

    def get_extra_info(self, name, default=None):
        """
        Return optional transport information.

        - `'related_address'`: the related address
        - `'sockname'`: the relayed address
        """
        if name == "related_address":
            return self.__inner_protocol.transport.get_extra_info("sockname")
        elif name == "sockname":
            return self.__relayed_address
        return default

    def sendto(self, data, addr):
        """
        Sends the `data` bytes to the remote peer given `addr`.

        This will bind a TURN channel as necessary.
        """
        asyncio.ensure_future(self.__inner_protocol.send_data(data, addr))

    async def _connect(self, retransmissions=None):
        self.__relayed_address = await self.__inner_protocol.connect(retransmissions)
        self.protocol.connection_made(self)


async def create_turn_endpoint(
    protocol_factory,
    server_addr,
    username,
    password,
    lifetime=600,
    ssl=False,
    transport="udp",
    retransmissions=None,
):
    """
    Create datagram connection relayed over TURN.
    """
    loop = asyncio.get_event_loop()
    if transport == "tcp":
        _, inner_protocol = await loop.create_connection(
            lambda: TurnClientTcpProtocol(
                server_addr, username=username, password=password, lifetime=lifetime
            ),
            host=server_addr[0],
            port=server_addr[1],
            ssl=ssl,
        )
    else:
        _, inner_protocol = await loop.create_datagram_endpoint(
            lambda: TurnClientUdpProtocol(
                server_addr, username=username, password=password, lifetime=lifetime
            ),
            remote_addr=server_addr,
        )

    protocol = protocol_factory()
    transport = TurnTransport(protocol, inner_protocol)
    await transport._connect(retransmissions)

    return transport, protocol
