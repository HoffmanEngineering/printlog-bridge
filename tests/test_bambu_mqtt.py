"""The Bambu adapter's MQTT client and its certificate pin, against `tests/fake_bambu.py`."""

from __future__ import annotations

import asyncio
import json

import pytest
from fake_bambu import ACCESS_CODE, SERIAL, FakeBambu, certificate

from printlog_bridge.adapters.bambu import mqtt
from printlog_bridge.adapters.bambu.tls import (
    CertificateChanged,
    CertificatePin,
    client_context,
    fingerprint,
)
from printlog_bridge.core.state import PrinterState


@pytest.fixture
def cert(tmp_path_factory):
    return certificate(tmp_path_factory.mktemp("tls"))


async def connect(broker: FakeBambu, password: str = ACCESS_CODE, **options) -> mqtt.MqttClient:
    return await mqtt.MqttClient.connect(
        "127.0.0.1",
        broker.port,
        client_context(),
        client_id="printlog-bridge-test",
        username="bblp",
        password=password,
        **options,
    )


def run(scenario):
    return asyncio.run(scenario)


def test_connects_with_the_access_code_subscribes_and_receives_reports(cert):
    async def scenario():
        broker = FakeBambu(*cert)
        await broker.start()
        client = await connect(broker)
        try:
            await client.subscribe(f"device/{SERIAL}/report")
            await client.publish(
                f"device/{SERIAL}/request",
                json.dumps({"pushing": {"command": "pushall", "sequence_id": "0"}}).encode(),
            )
            topic, payload = await asyncio.wait_for(client.receive(), 5)
            return broker.connects[0], topic, json.loads(payload)
        finally:
            await client.close()
            await broker.close()

    connect_packet, topic, message = run(scenario())

    assert connect_packet["protocol"] == "MQTT"
    assert connect_packet["level"] == 4
    assert connect_packet["clean"] is True
    assert connect_packet["username"] == "bblp"
    assert topic == f"device/{SERIAL}/report"
    assert message["print"]["gcode_state"] == "IDLE"


def test_a_wrong_access_code_is_refused_with_its_return_code(cert):
    async def scenario():
        broker = FakeBambu(*cert)
        await broker.start()
        try:
            await connect(broker, password="00000000")
        finally:
            await broker.close()

    with pytest.raises(mqtt.MqttRefused) as refused:
        run(scenario())

    assert refused.value.code == 5
    assert "not authorized" in str(refused.value)


def test_pings_keep_the_connection_and_a_silent_broker_is_given_up_on(cert):
    async def scenario():
        broker = FakeBambu(*cert)
        await broker.start()
        client = await connect(broker, keepalive=1)
        try:
            await asyncio.sleep(2.5)  # several pings: the broker answers each
            assert not client.closed
            broker.drop()
            with pytest.raises(mqtt.MqttClosed):
                await asyncio.wait_for(client.receive(), 5)
        finally:
            await client.close()
            await broker.close()

    run(scenario())


def test_a_large_report_arrives_whole(cert):
    async def scenario():
        broker = FakeBambu(*cert)
        await broker.start()
        client = await connect(broker)
        try:
            await client.subscribe(f"device/{SERIAL}/report")
            while not broker.subscribed:
                await asyncio.sleep(0.01)
            await broker.send({"note": "x" * 300_000})
            _, payload = await asyncio.wait_for(client.receive(), 5)
            return json.loads(payload)
        finally:
            await client.close()
            await broker.close()

    assert len(run(scenario())["print"]["note"]) == 300_000


def test_the_certificate_is_checked_before_the_access_code_is_sent(cert, tmp_path):
    async def scenario():
        broker = FakeBambu(*cert)
        await broker.start()

        def refuse(der: bytes) -> None:
            raise CertificateChanged("not the printer")

        try:
            with pytest.raises(CertificateChanged):
                await connect(broker, verify=refuse)
            return broker.connects
        finally:
            await broker.close()

    assert run(scenario()) == []


class TestCertificatePin:
    def test_the_first_certificate_is_pinned_and_kept_in_the_state(self, tmp_path):
        state = PrinterState.load(tmp_path, "x1c")
        CertificatePin("x1c", None, state).check(b"cert-one", "mqtt")

        again = PrinterState.load(tmp_path, "x1c")
        CertificatePin("x1c", None, again).check(b"cert-one", "mqtt")
        assert again.discovery["bambu_certificates"]["mqtt"] == fingerprint(b"cert-one")

    def test_another_certificate_is_refused_naming_its_fingerprint(self, tmp_path):
        state = PrinterState.load(tmp_path, "x1c")
        CertificatePin("x1c", None, state).check(b"cert-one", "mqtt")

        with pytest.raises(CertificateChanged) as changed:
            CertificatePin("x1c", None, state).check(b"cert-two", "mqtt")

        assert fingerprint(b"cert-two") in str(changed.value)
        assert "tls_fingerprint" in str(changed.value)

    def test_a_configured_fingerprint_wins_over_the_pinned_one(self, tmp_path):
        state = PrinterState.load(tmp_path, "x1c")
        CertificatePin("x1c", None, state).check(b"cert-one", "mqtt")

        pin = CertificatePin("x1c", fingerprint(b"cert-two").lower().replace(":", ""), state)
        pin.check(b"cert-two", "mqtt")
        with pytest.raises(CertificateChanged):
            pin.check(b"cert-one", "mqtt")
        assert state.discovery["bambu_certificates"]["mqtt"] == fingerprint(b"cert-two")

    def test_each_service_has_its_own_pin(self, tmp_path):
        pin = CertificatePin("x1c", None, PrinterState.load(tmp_path, "x1c"))
        pin.check(b"mqtt-cert", "mqtt")
        pin.check(b"ftps-cert", "ftps")

    def test_a_read_only_state_checks_but_pins_nothing(self, tmp_path):
        PrinterState.load(tmp_path, "x1c").save()
        state = PrinterState.peek(tmp_path, "x1c")
        assert state is not None

        CertificatePin("x1c", None, state).check(b"cert-one", "mqtt")

        assert "bambu_certificates" not in PrinterState.load(tmp_path, "x1c").discovery


def test_a_broker_that_hangs_up_on_connect_is_closed_not_refused(cert):
    async def scenario():
        broker = FakeBambu(*cert)
        broker.hang_up = True
        await broker.start()
        try:
            await connect(broker)
        finally:
            await broker.close()

    with pytest.raises(mqtt.MqttClosed):
        run(scenario())
