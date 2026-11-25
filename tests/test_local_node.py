"""Tests for the fixed local ECHONET Lite node objects."""

from pyhems import (
    CONTROLLER_INSTANCE,
    EOJ,
    ESV,
    NODE_PROFILE_INSTANCE,
    Frame,
    Property,
)
from pyhems.local_node import LocalNodeResponder


def _request(deoj: int, esv: ESV, *epcs: int) -> Frame:
    return Frame(
        tid=0x1234,
        seoj=EOJ(0x010101),
        deoj=EOJ(deoj),
        esv=esv,
        properties=[Property(epc=epc) for epc in epcs],
    )


def test_controller_responds_to_exact_and_all_instance_eoj() -> None:
    """The minimal Controller exposes only its required operation status."""
    responder = LocalNodeResponder()

    for deoj in (int(CONTROLLER_INSTANCE), 0x05FF00):
        response = responder.respond(_request(deoj, ESV.GET, 0x80))

        assert response is not None
        assert response.seoj == CONTROLLER_INSTANCE
        assert response.deoj == EOJ(0x010101)
        assert response.esv == ESV.GET_RES
        assert response.properties[0] == Property(epc=0x80, edt=b"\x30")


def test_controller_does_not_respond_to_unsupported_properties() -> None:
    """Unsupported Controller properties are intentionally left unanswered."""
    assert LocalNodeResponder().respond(_request(0x05FF00, ESV.GET, 0xC0)) is None


def test_node_profile_responds_to_exact_and_all_instance_eoj() -> None:
    """The required Node Profile values are returned as fixed EDTs."""
    responder = LocalNodeResponder()

    response = responder.respond(
        _request(0x0EF000, ESV.GET, 0x80, 0x83, 0xD3, 0xD4, 0xD6, 0xD7)
    )

    assert response is not None
    assert response.seoj == NODE_PROFILE_INSTANCE
    assert response.esv == ESV.GET_RES
    assert {property_.epc for property_ in response.properties} == {
        0x80,
        0x83,
        0xD3,
        0xD4,
        0xD6,
        0xD7,
    }
    assert response.properties[-2:] == [
        Property(epc=0xD6, edt=bytes.fromhex("0105FF01")),
        Property(epc=0xD7, edt=bytes.fromhex("0105FF")),
    ]


def test_node_profile_answers_inf_request() -> None:
    """INF_REQ receives a one-shot INF containing the requested values."""
    response = LocalNodeResponder().respond(_request(0x0EF001, ESV.INF_REQ, 0x80, 0x82))

    assert response is not None
    assert response.esv == ESV.INF
    assert response.seoj == NODE_PROFILE_INSTANCE
    assert [property_.epc for property_ in response.properties] == [0x80, 0x82]


def test_startup_announcement_lists_controller_instance() -> None:
    """Startup D5 is a single-property multicast notification."""
    frame = LocalNodeResponder().startup_announcement(tid=7)

    assert frame.tid == 7
    assert frame.seoj == NODE_PROFILE_INSTANCE
    assert frame.deoj == NODE_PROFILE_INSTANCE
    assert frame.esv == ESV.INF
    assert frame.properties == [Property(epc=0xD5, edt=bytes.fromhex("0105FF01"))]
