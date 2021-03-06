# -*- coding: utf-8 -*-
"""
test_basic_logic
~~~~~~~~~~~~~~~~

Test the basic logic of the h2 state machines.
"""
import random
import sys

import hyperframe
import pytest

import h2.connection
import h2.errors
import h2.events
import h2.exceptions
import h2.frame_buffer

import helpers


IS_PYTHON3 = sys.version_info >= (3, 0)


class TestBasicClient(object):
    """
    Basic client-side tests.
    """
    example_request_headers = [
        (':authority', 'example.com'),
        (':path', '/'),
        (':scheme', 'https'),
        (':method', 'GET'),
    ]
    example_response_headers = [
        (':status', '200'),
        ('server', 'fake-serv/0.1.0')
    ]

    def test_begin_connection(self, frame_factory):
        """
        Client connections emit the HTTP/2 preamble.
        """
        c = h2.connection.H2Connection()
        expected_settings = frame_factory.build_settings_frame(
            c.local_settings
        )
        expected_data = (
            b'PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n' + expected_settings.serialize()
        )

        events = c.initiate_connection()
        assert not events
        assert c.data_to_send() == expected_data

    def test_sending_headers(self):
        """
        Single headers frames are correctly encoded.
        """
        c = h2.connection.H2Connection()
        c.initiate_connection()

        # Clear the data, then send headers.
        c.clear_outbound_data_buffer()
        events = c.send_headers(1, self.example_request_headers)
        assert not events
        assert c.data_to_send() == (
            b'\x00\x00\r\x01\x04\x00\x00\x00\x01'
            b'A\x88/\x91\xd3]\x05\\\x87\xa7\x84\x87\x82'
        )

    def test_sending_data(self):
        """
        Single data frames are encoded correctly.
        """
        c = h2.connection.H2Connection()
        c.initiate_connection()
        c.send_headers(1, self.example_request_headers)

        # Clear the data, then send some data.
        c.clear_outbound_data_buffer()
        events = c.send_data(1, b'some data')
        assert not events
        assert (
            c.data_to_send() == b'\x00\x00\t\x00\x00\x00\x00\x00\x01some data'
        )

    def test_closing_stream_sending_data(self, frame_factory):
        """
        We can close a stream with a data frame.
        """
        c = h2.connection.H2Connection()
        c.initiate_connection()
        c.send_headers(1, self.example_request_headers)

        f = frame_factory.build_data_frame(
            data=b'some data',
            flags=['END_STREAM'],
        )

        # Clear the data, then send some data.
        c.clear_outbound_data_buffer()
        events = c.send_data(1, b'some data', end_stream=True)
        assert not events
        assert c.data_to_send() == f.serialize()

    def test_receiving_a_response(self, frame_factory):
        """
        When receiving a response, the ResponseReceived event fires.
        """
        c = h2.connection.H2Connection()
        c.initiate_connection()
        c.send_headers(1, self.example_request_headers, end_stream=True)

        # Clear the data
        f = frame_factory.build_headers_frame(
            self.example_response_headers
        )
        events = c.receive_data(f.serialize())

        assert len(events) == 1
        event = events[0]

        assert isinstance(event, h2.events.ResponseReceived)
        assert event.stream_id == 1
        assert event.headers == self.example_response_headers

    def test_end_stream_without_data(self, frame_factory):
        """
        Ending a stream without data emits a zero-length DATA frame with
        END_STREAM set.
        """
        c = h2.connection.H2Connection()
        c.initiate_connection()
        c.send_headers(1, self.example_request_headers, end_stream=False)

        # Clear the data
        c.clear_outbound_data_buffer()
        f = frame_factory.build_data_frame(b'', flags=['END_STREAM'])
        events = c.end_stream(1)

        assert not events
        assert c.data_to_send() == f.serialize()

    def test_cannot_send_headers_on_lower_stream_id(self):
        """
        Once stream ID x has been used, cannot use stream ID y where y < x.
        """
        c = h2.connection.H2Connection()
        c.initiate_connection()
        c.send_headers(3, self.example_request_headers, end_stream=False)

        with pytest.raises(ValueError):
            c.send_headers(1, self.example_request_headers, end_stream=True)

    def test_receiving_pushed_stream(self, frame_factory):
        """
        Pushed streams fire a PushedStreamReceived event, followed by
        ResponseReceived when the response headers are received.
        """
        c = h2.connection.H2Connection()
        c.initiate_connection()
        c.send_headers(1, self.example_request_headers, end_stream=False)

        f1 = frame_factory.build_headers_frame(
            self.example_response_headers
        )
        f2 = frame_factory.build_push_promise_frame(
            stream_id=1,
            promised_stream_id=2,
            headers=self.example_request_headers,
            flags=['END_HEADERS'],
        )
        f3 = frame_factory.build_headers_frame(
            self.example_response_headers,
            stream_id=2,
        )
        data = b''.join(x.serialize() for x in [f1, f2, f3])

        events = c.receive_data(data)

        assert len(events) == 3
        stream_push_event = events[1]
        response_event = events[2]
        assert isinstance(stream_push_event, h2.events.PushedStreamReceived)
        assert isinstance(response_event, h2.events.ResponseReceived)

        assert stream_push_event.pushed_stream_id == 2
        assert stream_push_event.parent_stream_id == 1
        assert (
            stream_push_event.headers == self.example_request_headers
        )
        assert response_event.stream_id == 2
        assert response_event.headers == self.example_response_headers

    def test_cannot_receive_pushed_stream_when_enable_push_is_0(self,
                                                                frame_factory):
        """
        If we have set SETTINGS_ENABLE_PUSH to 0, receiving PUSH_PROMISE frames
        triggers the connection to be closed.
        """
        c = h2.connection.H2Connection()
        c.initiate_connection()
        c.local_settings.enable_push = 0
        c.send_headers(1, self.example_request_headers, end_stream=False)

        f1 = frame_factory.build_settings_frame({}, ack=True)
        f2 = frame_factory.build_headers_frame(
            self.example_response_headers
        )
        f3 = frame_factory.build_push_promise_frame(
            stream_id=1,
            promised_stream_id=2,
            headers=self.example_request_headers,
            flags=['END_HEADERS'],
        )
        c.receive_data(f1.serialize())
        c.receive_data(f2.serialize())
        c.clear_outbound_data_buffer()

        with pytest.raises(h2.exceptions.ProtocolError):
            c.receive_data(f3.serialize())

        expected_frame = frame_factory.build_goaway_frame(
            1, h2.errors.PROTOCOL_ERROR
        )
        assert c.data_to_send() == expected_frame.serialize()

    def test_receiving_response_no_body(self, frame_factory):
        """
        Receiving a response without a body fires two events, ResponseReceived
        and StreamEnded.
        """
        c = h2.connection.H2Connection()
        c.initiate_connection()
        c.send_headers(1, self.example_request_headers, end_stream=True)

        f = frame_factory.build_headers_frame(
            self.example_response_headers,
            flags=['END_STREAM']
        )
        events = c.receive_data(f.serialize())

        assert len(events) == 2
        response_event = events[0]
        end_stream = events[1]

        assert isinstance(response_event, h2.events.ResponseReceived)
        assert isinstance(end_stream, h2.events.StreamEnded)

    def test_oversize_headers(self):
        """
        Sending headers that are oversized generates a stream of CONTINUATION
        frames.
        """
        all_bytes = [chr(x) for x in range(0, 256)]
        if IS_PYTHON3:
            all_bytes = [x.encode('latin1') for x in all_bytes]

        large_binary_string = b''.join(
            random.choice(all_bytes) for _ in range(0, 256)
        )
        test_headers = {'key': large_binary_string}
        c = h2.connection.H2Connection()

        # Greatly shrink the max frame size to force us over.
        c.max_outbound_frame_size = 48
        c.initiate_connection()
        c.send_headers(1, test_headers, end_stream=True)

        # Use the frame buffer here, because we don't care about decoding
        # the headers.
        buffer = h2.frame_buffer.FrameBuffer(server=True)
        buffer.add_data(c.data_to_send())
        frames = list(buffer)

        # Remove a settings frame.
        frames.pop(0)
        assert len(frames) > 1
        headers_frame = frames[0]
        continuation_frames = frames[1:]

        assert isinstance(headers_frame, hyperframe.frame.HeadersFrame)
        assert all(
            map(
                lambda f: isinstance(f, hyperframe.frame.ContinuationFrame),
                continuation_frames)
        )

        assert frames[0].flags == set(['END_STREAM'])
        assert frames[-1].flags == set(['END_HEADERS'])

        assert all(
            map(lambda f: len(f.data) <= c.max_outbound_frame_size, frames)
        )

    def test_handle_stream_reset(self, frame_factory):
        """
        Streams being remotely reset fires a StreamReset event.
        """
        c = h2.connection.H2Connection()
        c.initiate_connection()
        c.send_headers(1, self.example_request_headers, end_stream=True)
        c.clear_outbound_data_buffer()

        f = frame_factory.build_rst_stream_frame(
            stream_id=1,
            error_code=5
        )
        events = c.receive_data(f.serialize())

        assert len(events) == 1
        event = events[0]

        assert isinstance(event, h2.events.StreamReset)
        assert event.stream_id == 1
        assert event.error_code == 5

    def test_can_consume_partial_data_from_connection(self):
        """
        We can do partial reads from the connection.
        """
        c = h2.connection.H2Connection()
        c.initiate_connection()

        assert len(c.data_to_send(2)) == 2
        assert len(c.data_to_send(3)) == 3
        assert 0 < len(c.data_to_send(500)) < 500
        assert len(c.data_to_send(10)) == 0
        assert len(c.data_to_send()) == 0

    def test_we_can_update_settings(self, frame_factory):
        """
        Updating the settings emits a SETTINGS frame.
        """
        c = h2.connection.H2Connection()
        c.initiate_connection()
        c.clear_outbound_data_buffer()

        new_settings = {
            hyperframe.frame.SettingsFrame.HEADER_TABLE_SIZE: 52,
            hyperframe.frame.SettingsFrame.ENABLE_PUSH: 0,
        }
        events = c.update_settings(new_settings)
        assert not events

        f = frame_factory.build_settings_frame(new_settings)
        assert c.data_to_send() == f.serialize()

    def test_settings_get_acked_correctly(self, frame_factory):
        """
        When settings changes are ACKed, they contain the changed settings.
        """
        c = h2.connection.H2Connection()
        c.initiate_connection()

        new_settings = {
            hyperframe.frame.SettingsFrame.HEADER_TABLE_SIZE: 52,
            hyperframe.frame.SettingsFrame.ENABLE_PUSH: 0,
        }
        c.update_settings(new_settings)

        f = frame_factory.build_settings_frame({}, ack=True)
        events = c.receive_data(f.serialize())

        assert len(events) == 1
        event = events[0]

        assert isinstance(event, h2.events.SettingsAcknowledged)
        assert len(event.changed_settings) == len(new_settings)
        for setting, value in new_settings.items():
            assert event.changed_settings[setting].new_value == value

    def test_cannot_create_new_outbound_stream_over_limit(self, frame_factory):
        """
        When the number of outbound streams exceeds the remote peer's
        MAX_CONCURRENT_STREAMS setting, attempting to open new streams fails.
        """
        c = h2.connection.H2Connection()
        c.initiate_connection()

        f = frame_factory.build_settings_frame(
            {hyperframe.frame.SettingsFrame.MAX_CONCURRENT_STREAMS: 1}
        )
        event = c.receive_data(f.serialize())[0]
        c.acknowledge_settings(event)

        c.send_headers(1, self.example_request_headers)

        with pytest.raises(h2.exceptions.TooManyStreamsError):
            c.send_headers(3, self.example_request_headers)

    def test_can_receive_trailers(self, frame_factory):
        """
        When two HEADERS blocks are received in the same stream from a
        server, the second set are trailers.
        """
        c = h2.connection.H2Connection()
        c.initiate_connection()
        c.send_headers(1, self.example_request_headers)
        f = frame_factory.build_headers_frame(self.example_response_headers)
        c.receive_data(f.serialize())

        # Send in trailers.
        trailers = [('content-length', '0')]
        f = frame_factory.build_headers_frame(
            trailers,
            flags=['END_STREAM'],
        )
        events = c.receive_data(f.serialize())
        assert len(events) == 2

        event = events[0]
        assert isinstance(event, h2.events.TrailersReceived)
        assert event.headers == trailers
        assert event.stream_id == 1

    def test_reject_trailers_not_ending_stream(self, frame_factory):
        """
        When trailers are received without the END_STREAM flag being present,
        this is a ProtocolError.
        """
        c = h2.connection.H2Connection()
        c.initiate_connection()
        c.send_headers(1, self.example_request_headers)
        f = frame_factory.build_headers_frame(self.example_request_headers)
        c.receive_data(f.serialize())

        # Send in trailers.
        c.clear_outbound_data_buffer()
        trailers = [('content-length', '0')]
        f = frame_factory.build_headers_frame(
            trailers,
            flags=[],
        )

        with pytest.raises(h2.exceptions.ProtocolError):
            c.receive_data(f.serialize())

        expected_frame = frame_factory.build_goaway_frame(
            last_stream_id=1, error_code=h2.errors.PROTOCOL_ERROR,
        )
        assert c.data_to_send() == expected_frame.serialize()

    def test_can_send_trailers(self, frame_factory):
        """
        When a second set of headers are sent, they are properly trailers.
        """
        c = h2.connection.H2Connection()
        c.initiate_connection()
        c.clear_outbound_data_buffer()
        c.send_headers(1, self.example_request_headers)

        # Now send trailers.
        trailers = [('content-length', '0')]
        c.send_headers(1, trailers, end_stream=True)

        frame_factory.refresh_encoder()
        f1 = frame_factory.build_headers_frame(
            self.example_request_headers,
        )
        f2 = frame_factory.build_headers_frame(
            trailers,
            flags=['END_STREAM'],
        )
        assert c.data_to_send() == f1.serialize() + f2.serialize()

    def test_trailers_must_have_end_stream(self, frame_factory):
        """
        A set of trailers must carry the END_STREAM flag.
        """
        c = h2.connection.H2Connection()
        c.initiate_connection()

        # Send headers.
        c.send_headers(1, self.example_request_headers)

        # Now send trailers.
        trailers = [('content-length', '0')]

        with pytest.raises(h2.exceptions.ProtocolError):
            c.send_headers(1, trailers)


class TestBasicServer(object):
    """
    Basic server-side tests.
    """
    example_request_headers = [
        (':authority', 'example.com'),
        (':path', '/'),
        (':scheme', 'https'),
        (':method', 'GET'),
    ]
    example_response_headers = [
        (':status', '200'),
        ('server', 'hyper-h2/0.1.0')
    ]

    def test_ignores_preamble(self):
        """
        The preamble does not cause any events or frames to be written.
        """
        c = h2.connection.H2Connection(client_side=False)
        preamble = b'PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n'

        events = c.receive_data(preamble)
        assert not events
        assert not c.data_to_send()

    @pytest.mark.parametrize("chunk_size", range(1, 24))
    def test_drip_feed_preamble(self, chunk_size):
        """
        The preamble can be sent in in less than a single buffer.
        """
        c = h2.connection.H2Connection(client_side=False)
        preamble = b'PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n'
        events = []

        for i in range(0, len(preamble), chunk_size):
            events += c.receive_data(preamble[i:i+chunk_size])

        assert not events
        assert not c.data_to_send()

    def test_initiate_connection_sends_server_preamble(self, frame_factory):
        """
        For server-side connections, initiate_connection sends a server
        preamble.
        """
        c = h2.connection.H2Connection(client_side=False)
        expected_settings = frame_factory.build_settings_frame(
            c.local_settings
        )
        expected_data = expected_settings.serialize()

        events = c.initiate_connection()
        assert not events
        assert c.data_to_send() == expected_data

    def test_headers_event(self, frame_factory):
        """
        When a headers frame is received a RequestReceived event fires.
        """
        c = h2.connection.H2Connection(client_side=False)
        c.receive_data(frame_factory.preamble())

        f = frame_factory.build_headers_frame(self.example_request_headers)
        data = f.serialize()
        events = c.receive_data(data)

        assert len(events) == 1
        event = events[0]

        assert isinstance(event, h2.events.RequestReceived)
        assert event.stream_id == 1
        assert event.headers == self.example_request_headers

    def test_data_event(self, frame_factory):
        """
        Test that data received on a stream fires a DataReceived event.
        """
        c = h2.connection.H2Connection(client_side=False)
        c.receive_data(frame_factory.preamble())

        f1 = frame_factory.build_headers_frame(
            self.example_request_headers, stream_id=3
        )
        f2 = frame_factory.build_data_frame(
            b'some request data',
            stream_id=3,
        )
        data = b''.join(map(lambda f: f.serialize(), [f1, f2]))
        events = c.receive_data(data)

        assert len(events) == 2
        event = events[1]

        assert isinstance(event, h2.events.DataReceived)
        assert event.stream_id == 3
        assert event.data == b'some request data'

    def test_receiving_ping_frame(self, frame_factory):
        """
        Ping frames should be immediately ACKed.
        """
        c = h2.connection.H2Connection(client_side=False)
        c.receive_data(frame_factory.preamble())

        ping_data = b'\x01' * 8
        sent_frame = frame_factory.build_ping_frame(ping_data)
        expected_frame = frame_factory.build_ping_frame(
            ping_data, flags=["ACK"]
        )
        expected_data = expected_frame.serialize()

        c.clear_outbound_data_buffer()
        events = c.receive_data(sent_frame.serialize())

        assert not events
        assert c.data_to_send() == expected_data

    def test_receiving_settings_frame_event(self, frame_factory):
        """
        Settings frames should cause a RemoteSettingsChanged event to fire.
        """
        c = h2.connection.H2Connection(client_side=False)
        c.receive_data(frame_factory.preamble())

        f = frame_factory.build_settings_frame(
            settings=helpers.SAMPLE_SETTINGS
        )
        events = c.receive_data(f.serialize())

        assert len(events) == 1
        event = events[0]

        assert isinstance(event, h2.events.RemoteSettingsChanged)
        assert len(event.changed_settings) == len(helpers.SAMPLE_SETTINGS)

    def test_acknowledging_settings(self, frame_factory):
        """
        Acknowledging settings causes appropriate Settings frame to be emitted.
        """
        c = h2.connection.H2Connection(client_side=False)
        c.receive_data(frame_factory.preamble())

        received_frame = frame_factory.build_settings_frame(
            settings=helpers.SAMPLE_SETTINGS
        )
        expected_frame = frame_factory.build_settings_frame(
            settings={}, ack=True
        )
        expected_data = expected_frame.serialize()

        event = c.receive_data(received_frame.serialize())[0]
        c.clear_outbound_data_buffer()
        events = c.acknowledge_settings(event)

        assert not events
        assert c.data_to_send() == expected_data

    def test_close_connection(self, frame_factory):
        """
        Closing the connection with no error code emits a GOAWAY frame with
        error code 0.
        """
        c = h2.connection.H2Connection(client_side=False)
        c.receive_data(frame_factory.preamble())
        f = frame_factory.build_goaway_frame(last_stream_id=0)
        expected_data = f.serialize()

        c.clear_outbound_data_buffer()
        events = c.close_connection()

        assert not events
        assert c.data_to_send() == expected_data

    @pytest.mark.parametrize("error_code", h2.errors.H2_ERRORS)
    def test_close_connection_with_error_code(self, frame_factory, error_code):
        """
        Closing the connection with an error code emits a GOAWAY frame with
        that error code.
        """
        c = h2.connection.H2Connection(client_side=False)
        c.receive_data(frame_factory.preamble())
        f = frame_factory.build_goaway_frame(
            error_code=error_code, last_stream_id=0
        )
        expected_data = f.serialize()

        c.clear_outbound_data_buffer()
        events = c.close_connection(error_code)

        assert not events
        assert c.data_to_send() == expected_data

    def test_reset_stream(self, frame_factory):
        """
        Resetting a stream with no error code emits a RST_STREAM frame with
        error code 0.
        """
        c = h2.connection.H2Connection(client_side=False)
        c.receive_data(frame_factory.preamble())
        f = frame_factory.build_headers_frame(self.example_request_headers)
        c.receive_data(f.serialize())
        c.clear_outbound_data_buffer()

        expected_frame = frame_factory.build_rst_stream_frame(stream_id=1)
        expected_data = expected_frame.serialize()

        events = c.reset_stream(stream_id=1)

        assert not events
        assert c.data_to_send() == expected_data

    @pytest.mark.parametrize("error_code", h2.errors.H2_ERRORS)
    def test_reset_stream_with_error_code(self, frame_factory, error_code):
        """
        Resetting a stream with an error code emits a RST_STREAM frame with
        that error code.
        """
        c = h2.connection.H2Connection(client_side=False)
        c.receive_data(frame_factory.preamble())
        f = frame_factory.build_headers_frame(
            self.example_request_headers,
            stream_id=3
        )
        c.receive_data(f.serialize())
        c.clear_outbound_data_buffer()

        expected_frame = frame_factory.build_rst_stream_frame(
            stream_id=3, error_code=error_code
        )
        expected_data = expected_frame.serialize()

        events = c.reset_stream(stream_id=3, error_code=error_code)

        assert not events
        assert c.data_to_send() == expected_data

    def test_cannot_reset_nonexistent_stream(self, frame_factory):
        """
        Resetting nonexistent streams raises NoSuchStreamError.
        """
        c = h2.connection.H2Connection(client_side=False)
        c.receive_data(frame_factory.preamble())
        f = frame_factory.build_headers_frame(
            self.example_request_headers,
            stream_id=3
        )
        c.receive_data(f.serialize())

        with pytest.raises(h2.exceptions.NoSuchStreamError) as e:
            c.reset_stream(stream_id=1)

        assert e.value.stream_id == 1

        with pytest.raises(h2.exceptions.NoSuchStreamError) as e:
            c.reset_stream(stream_id=5)

        assert e.value.stream_id == 5

    def test_basic_sending_ping_frame_logic(self, frame_factory):
        """
        Sending ping frames serializes a ping frame on stream 0 with
        approriate opaque data.
        """
        c = h2.connection.H2Connection(client_side=False)
        c.receive_data(frame_factory.preamble())
        c.clear_outbound_data_buffer()

        ping_data = b'\x01\x02\x03\x04\x05\x06\x07\x08'

        expected_frame = frame_factory.build_ping_frame(ping_data)
        expected_data = expected_frame.serialize()

        events = c.ping(ping_data)

        assert not events
        assert c.data_to_send() == expected_data

    @pytest.mark.parametrize(
        'opaque_data',
        [
            b'',
            b'\x01\x02\x03\x04\x05\x06\x07',
            u'abcdefgh',
            b'too many bytes',
        ]
    )
    def test_ping_frame_opaque_data_must_be_length_8_bytestring(self,
                                                                frame_factory,
                                                                opaque_data):
        """
        Sending a ping frame only works with 8-byte bytestrings.
        """
        c = h2.connection.H2Connection(client_side=False)
        c.receive_data(frame_factory.preamble())

        with pytest.raises(ValueError):
            c.ping(opaque_data)

    def test_receiving_ping_acknowledgement(self, frame_factory):
        """
        Receiving a PING acknolwedgement fires a PingAcknolwedged event.
        """
        c = h2.connection.H2Connection(client_side=False)
        c.receive_data(frame_factory.preamble())

        ping_data = b'\x01\x02\x03\x04\x05\x06\x07\x08'

        f = frame_factory.build_ping_frame(
            ping_data, flags=['ACK']
        )
        events = c.receive_data(f.serialize())

        assert len(events) == 1
        event = events[0]

        assert isinstance(event, h2.events.PingAcknowledged)
        assert event.ping_data == ping_data

    def test_stream_ended_remotely(self, frame_factory):
        """
        When the remote stream ends with a non-empty data frame a DataReceived
        event and a StreamEnded event are fired.
        """
        c = h2.connection.H2Connection(client_side=False)
        c.receive_data(frame_factory.preamble())

        f1 = frame_factory.build_headers_frame(
            self.example_request_headers, stream_id=3
        )
        f2 = frame_factory.build_data_frame(
            b'some request data',
            flags=['END_STREAM'],
            stream_id=3,
        )
        data = b''.join(map(lambda f: f.serialize(), [f1, f2]))
        events = c.receive_data(data)

        assert len(events) == 3
        data_event = events[1]
        stream_ended_event = events[2]

        assert isinstance(data_event, h2.events.DataReceived)
        assert isinstance(stream_ended_event, h2.events.StreamEnded)
        stream_ended_event.stream_id == 3

    def test_can_push_stream(self, frame_factory):
        """
        Pushing a stream causes a PUSH_PROMISE frame to be emitted.
        """
        c = h2.connection.H2Connection(client_side=False)
        c.receive_data(frame_factory.preamble())
        f = frame_factory.build_headers_frame(
            self.example_request_headers
        )
        c.receive_data(f.serialize())

        frame_factory.refresh_encoder()
        expected_frame = frame_factory.build_push_promise_frame(
            stream_id=1,
            promised_stream_id=2,
            headers=self.example_request_headers,
            flags=['END_HEADERS'],
        )

        c.clear_outbound_data_buffer()
        c.push_stream(
            stream_id=1,
            promised_stream_id=2,
            request_headers=self.example_request_headers
        )

        assert c.data_to_send() == expected_frame.serialize()

    def test_cannot_push_streams_when_disabled(self, frame_factory):
        """
        When the remote peer has disabled stream pushing, we should fail.
        """
        c = h2.connection.H2Connection(client_side=False)
        c.receive_data(frame_factory.preamble())
        f = frame_factory.build_settings_frame(
            {hyperframe.frame.SettingsFrame.ENABLE_PUSH: 0}
        )
        events = c.receive_data(f.serialize())
        c.acknowledge_settings(events[0])

        f = frame_factory.build_headers_frame(
            self.example_request_headers
        )
        c.receive_data(f.serialize())

        with pytest.raises(h2.exceptions.ProtocolError):
            c.push_stream(
                stream_id=1,
                promised_stream_id=2,
                request_headers=self.example_request_headers
            )

    def test_settings_remote_change_header_table_size(self, frame_factory):
        """
        Acknowledging a remote HEADER_TABLE_SIZE settings change causes us to
        change the header table size of our encoder.
        """
        c = h2.connection.H2Connection(client_side=False)
        c.receive_data(frame_factory.preamble())

        assert c.encoder.header_table_size == 4096

        received_frame = frame_factory.build_settings_frame(
            {hyperframe.frame.SettingsFrame.HEADER_TABLE_SIZE: 80}
        )
        event = c.receive_data(received_frame.serialize())[0]
        c.clear_outbound_data_buffer()

        assert c.encoder.header_table_size == 4096

        c.acknowledge_settings(event)

        assert c.encoder.header_table_size == 80

    def test_settings_local_change_header_table_size(self, frame_factory):
        """
        The remote peer acknowledging a local HEADER_TABLE_SIZE settings change
        does not cause us to change the header table size of our decoder.

        For an explanation of why this test is this way around, see issue #37.
        """
        c = h2.connection.H2Connection(client_side=False)
        c.receive_data(frame_factory.preamble())

        assert c.decoder.header_table_size == 4096

        expected_frame = frame_factory.build_settings_frame({}, ack=True)
        c.update_settings(
            {hyperframe.frame.SettingsFrame.HEADER_TABLE_SIZE: 80}
        )
        c.receive_data(expected_frame.serialize())
        c.clear_outbound_data_buffer()

        assert c.decoder.header_table_size == 4096

    def test_restricting_outbound_frame_size_by_settings(self, frame_factory):
        """
        The remote peer can shrink the maximum outbound frame size using
        settings.
        """
        c = h2.connection.H2Connection(client_side=False)
        c.receive_data(frame_factory.preamble())
        c.send_headers(1, self.example_request_headers)

        with pytest.raises(h2.exceptions.FrameTooLargeError):
            c.send_data(1, b'\x01' * 17000)

        received_frame = frame_factory.build_settings_frame(
            {hyperframe.frame.SettingsFrame.SETTINGS_MAX_FRAME_SIZE: 17001}
        )
        event = c.receive_data(received_frame.serialize())[0]
        c.acknowledge_settings(event)
        c.clear_outbound_data_buffer()

        c.send_data(1, b'\x01' * 17000)
        assert c.data_to_send()

    def test_restricting_inbound_frame_size_by_settings(self, frame_factory):
        """
        We throw ProtocolErrors and tear down connections if oversize frames
        are received.
        """
        c = h2.connection.H2Connection(client_side=False)
        c.receive_data(frame_factory.preamble())
        h = frame_factory.build_headers_frame(self.example_request_headers)
        c.receive_data(h.serialize())
        c.clear_outbound_data_buffer()

        data_frame = frame_factory.build_data_frame(b'\x01' * 17000)

        with pytest.raises(h2.exceptions.ProtocolError):
            c.receive_data(data_frame.serialize())

        expected_frame = frame_factory.build_goaway_frame(
            last_stream_id=1, error_code=h2.errors.PROTOCOL_ERROR
        )
        assert c.data_to_send() == expected_frame.serialize()

    def test_cannot_receive_new_streams_over_limit(self, frame_factory):
        """
        When the number of inbound streams exceeds our MAX_CONCURRENT_STREAMS
        setting, their attempt to open new streams fails.
        """
        c = h2.connection.H2Connection(client_side=False)
        c.receive_data(frame_factory.preamble())
        c.update_settings(
            {hyperframe.frame.SettingsFrame.MAX_CONCURRENT_STREAMS: 1}
        )
        f = frame_factory.build_settings_frame({}, ack=True)
        c.receive_data(f.serialize())

        f = frame_factory.build_headers_frame(
            stream_id=1,
            headers=self.example_request_headers,
        )
        c.receive_data(f.serialize())
        c.clear_outbound_data_buffer()

        f = frame_factory.build_headers_frame(
            stream_id=3,
            headers=self.example_request_headers,
        )
        with pytest.raises(h2.exceptions.TooManyStreamsError):
            c.receive_data(f.serialize())

        expected_frame = frame_factory.build_goaway_frame(
            last_stream_id=1, error_code=h2.errors.PROTOCOL_ERROR,
        )
        assert c.data_to_send() == expected_frame.serialize()

    def test_can_receive_trailers(self, frame_factory):
        """
        When two HEADERS blocks are received in the same stream from a
        client, the second set are trailers.
        """
        c = h2.connection.H2Connection(client_side=False)
        c.receive_data(frame_factory.preamble())
        f = frame_factory.build_headers_frame(self.example_request_headers)
        c.receive_data(f.serialize())

        # Send in trailers.
        trailers = [('content-length', '0')]
        f = frame_factory.build_headers_frame(
            trailers,
            flags=['END_STREAM'],
        )
        events = c.receive_data(f.serialize())
        assert len(events) == 2

        event = events[0]
        assert isinstance(event, h2.events.TrailersReceived)
        assert event.headers == trailers
        assert event.stream_id == 1

    def test_reject_trailers_not_ending_stream(self, frame_factory):
        """
        When trailers are received without the END_STREAM flag being present,
        this is a ProtocolError.
        """
        c = h2.connection.H2Connection(client_side=False)
        c.receive_data(frame_factory.preamble())
        f = frame_factory.build_headers_frame(self.example_request_headers)
        c.receive_data(f.serialize())

        # Send in trailers.
        c.clear_outbound_data_buffer()
        trailers = [('content-length', '0')]
        f = frame_factory.build_headers_frame(
            trailers,
            flags=[],
        )

        with pytest.raises(h2.exceptions.ProtocolError):
            c.receive_data(f.serialize())

        expected_frame = frame_factory.build_goaway_frame(
            last_stream_id=1, error_code=h2.errors.PROTOCOL_ERROR,
        )
        assert c.data_to_send() == expected_frame.serialize()

    def test_can_send_trailers(self, frame_factory):
        """
        When a second set of headers are sent, they are properly trailers.
        """
        c = h2.connection.H2Connection(client_side=False)
        c.receive_data(frame_factory.preamble())
        f = frame_factory.build_headers_frame(self.example_request_headers)
        c.receive_data(f.serialize())

        # Send headers.
        c.clear_outbound_data_buffer()
        c.send_headers(1, self.example_response_headers)

        # Now send trailers.
        trailers = [('content-length', '0')]
        c.send_headers(1, trailers, end_stream=True)

        frame_factory.refresh_encoder()
        f1 = frame_factory.build_headers_frame(
            self.example_response_headers,
        )
        f2 = frame_factory.build_headers_frame(
            trailers,
            flags=['END_STREAM'],
        )
        assert c.data_to_send() == f1.serialize() + f2.serialize()

    def test_trailers_must_have_end_stream(self, frame_factory):
        """
        A set of trailers must carry the END_STREAM flag.
        """
        c = h2.connection.H2Connection(client_side=False)
        c.receive_data(frame_factory.preamble())
        f = frame_factory.build_headers_frame(self.example_request_headers)
        c.receive_data(f.serialize())

        # Send headers.
        c.send_headers(1, self.example_response_headers)

        # Now send trailers.
        trailers = [('content-length', '0')]

        with pytest.raises(h2.exceptions.ProtocolError):
            c.send_headers(1, trailers)

    @pytest.mark.parametrize("frame_id", range(12, 256))
    def test_unknown_frames_are_ignored(self, frame_factory, frame_id):
        c = h2.connection.H2Connection(client_side=False)
        c.receive_data(frame_factory.preamble())
        c.clear_outbound_data_buffer()

        f = frame_factory.build_data_frame(data=b'abcdefghtdst')
        f.type = frame_id

        events = c.receive_data(f.serialize())
        assert not events
        assert not c.data_to_send()

    def test_can_send_goaway_repeatedly(self, frame_factory):
        """
        We can send a GOAWAY frame as many times as we like.
        """
        c = h2.connection.H2Connection(client_side=False)
        c.receive_data(frame_factory.preamble())
        c.clear_outbound_data_buffer()

        c.close_connection()
        c.close_connection()
        c.close_connection()

        f = frame_factory.build_goaway_frame(last_stream_id=0)

        assert c.data_to_send() == (f.serialize() * 3)

    def test_receiving_goaway_frame(self, frame_factory):
        """
        Receiving a GOAWAY frame causes a ConnectionTerminated event to be
        fired and transitions the connection to the CLOSED state, and clears
        the outbound data buffer.
        """
        c = h2.connection.H2Connection(client_side=False)
        c.initiate_connection()
        c.receive_data(frame_factory.preamble())

        f = frame_factory.build_goaway_frame(last_stream_id=5, error_code=4)
        events = c.receive_data(f.serialize())

        assert len(events) == 1
        event = events[0]

        assert isinstance(event, h2.events.ConnectionTerminated)
        assert event.error_code == 4
        assert event.last_stream_id == 5
        assert c.state_machine.state == h2.connection.ConnectionState.CLOSED

        assert not c.data_to_send()

    def test_receiving_multiple_goaway_frames(self, frame_factory):
        """
        Multiple GOAWAY frames can be received at once, and are allowed. Each
        one fires a ConnectionTerminated event.
        """
        c = h2.connection.H2Connection(client_side=False)
        c.receive_data(frame_factory.preamble())
        c.clear_outbound_data_buffer()

        f = frame_factory.build_goaway_frame(last_stream_id=0)
        events = c.receive_data(f.serialize() * 3)

        assert len(events) == 3
        assert all(
            isinstance(event, h2.events.ConnectionTerminated)
            for event in events
        )
