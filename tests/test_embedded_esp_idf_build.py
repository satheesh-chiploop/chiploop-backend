import os
import pytest

os.environ.setdefault("SUPABASE_URL", "http://localhost:54321")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-service-role-key")

from agents.embedded import embedded_esp_idf_build as esp_build
from agents.embedded.embedded_esp_idf_build import _main_source


def test_esp_idf_source_uses_governed_board_contract_and_frame_width():
    source = _main_source(
        {"esp32_gpio": {"sclk": 14, "cs_n": 13, "mosi": 15, "miso": 2}},
        17,
    )
    assert "#define CHIPLOOP_PIN_SCLK 14" in source
    assert "#define CHIPLOOP_PIN_CS 13" in source
    assert "#define CHIPLOOP_FRAME_BYTES 17" in source
    assert "#define CHIPLOOP_SPI_CLOCK_HZ 10000000" in source
    assert "SPI2_HOST" in source
    assert "spi_device_transmit" in source
    assert "MALLOC_CAP_DMA" in source
    assert "xSemaphoreTake(chiploop_exchange_lock, portMAX_DELAY)" in source
    assert "xSemaphoreGive(chiploop_exchange_lock)" in source
    assert "memcpy(response, chiploop_rx_dma" in source
    assert "esp_rom_delay_us(1)" in source


def test_esp_idf_source_generates_register_transactions_from_transport_map():
    transport = {
        "serialized_output_bits": 33,
        "input_bit_map": [
            {"port": "reg_valid", "lsb": 0, "width": 1},
            {"port": "reg_we", "lsb": 1, "width": 1},
            {"port": "reg_addr", "lsb": 2, "width": 8},
            {"port": "reg_wdata", "lsb": 10, "width": 32},
        ],
        "output_bit_map": [
            {"port": "reg_rdata", "lsb": 1, "width": 32},
            {"port": "reg_ready", "lsb": 0, "width": 1},
        ],
    }
    source = _main_source({"esp32_gpio": {}}, 6, transport)
    assert "chiploop_register_write" in source
    assert "chiploop_register_read" in source
    assert "pipeline frame 1" in source
    assert "REG_RDATA_LSB 1" in source
    assert 'ESP_RETURN_ON_FALSE(frame_get_u32(response, REG_READY_LSB, 1)' in source


def _state(tmp_path, transport):
    transport = {
        "input_bit_map": [
            {"port": "reg_valid", "lsb": 0, "width": 1},
            {"port": "reg_we", "lsb": 1, "width": 1},
            {"port": "reg_addr", "lsb": 2, "width": 8},
            {"port": "reg_wdata", "lsb": 10, "width": 32},
        ],
        "output_bit_map": [
            {"port": "reg_rdata", "lsb": 1, "width": 32},
            {"port": "reg_ready", "lsb": 0, "width": 1},
        ],
        **transport,
    }
    return {
        "workflow_id": "wf", "workflow_dir": str(tmp_path),
        "target_refinement": {
            "compute_host": {"fabric_interface": {
                "protocol": "spi_register",
                "esp32_gpio": {"sclk": 14, "cs_n": 13, "mosi": 15, "miso": 2},
            }},
            "transport_contract": transport,
        },
    }


def test_esp_idf_build_rejects_inconsistent_frame_metadata(tmp_path):
    with pytest.raises(RuntimeError, match="inconsistent byte-aligned"):
        esp_build.run_esp_idf_build(_state(tmp_path, {
            "serialized_input_bits": 108, "serialized_output_bits": 132,
            "frame_bits": 132, "frame_bytes": 17, "response_latency_frames": 2,
        }))


def test_esp_idf_build_fails_closed_without_qualified_compiler(tmp_path, monkeypatch):
    monkeypatch.setattr(esp_build, "write_artifact", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(esp_build, "_idf_tool", lambda: None)
    with pytest.raises(RuntimeError, match="no alternate compiler"):
        esp_build.run_esp_idf_build(_state(tmp_path, {
            "serialized_input_bits": 108, "serialized_output_bits": 132,
            "frame_bits": 136, "frame_bytes": 17, "response_latency_frames": 2,
        }))
