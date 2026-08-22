import json
import os
import shutil
import subprocess

from ._embedded_common import ensure_workflow_dir, write_artifact


def _idf_tool() -> str | None:
    direct = shutil.which("idf.py")
    if direct:
        return direct
    root = str(os.getenv("IDF_PATH") or "").strip()
    candidate = os.path.join(root, "tools", "idf.py") if root else ""
    return candidate if candidate and os.path.isfile(candidate) else None


def _field(bit_map: list, name: str) -> dict:
    return next((item for item in bit_map if str(item.get("port") or "").lower() == name), {})


_REGISTER_FIELD_ALIASES = {
    "valid": ("reg_valid", "mmio_valid", "csr_valid"),
    "write": ("reg_we", "reg_write", "mmio_write", "mmio_we", "csr_write", "csr_we"),
    "address": ("reg_addr", "mmio_addr", "csr_addr"),
    "write_data": ("reg_wdata", "mmio_wdata", "csr_wdata"),
    "read_data": ("reg_rdata", "mmio_rdata", "csr_rdata"),
    "ready": ("reg_ready", "mmio_ready", "csr_ready"),
}


def _register_fields(inputs: list, outputs: list) -> dict[str, dict]:
    """Resolve conventional register-bus port names by semantic role."""
    input_roles = {"valid", "write", "address", "write_data"}
    resolved: dict[str, dict] = {}
    for role, aliases in _REGISTER_FIELD_ALIASES.items():
        bit_map = inputs if role in input_roles else outputs
        resolved[role] = next((field for alias in aliases if (field := _field(bit_map, alias))), {})
    return resolved


def _main_source(interface: dict, frame_bytes: int, transport: dict | None = None) -> str:
    gpio = interface.get("esp32_gpio") if isinstance(interface.get("esp32_gpio"), dict) else {}
    clock_hz = int(float(interface.get("maximum_clock_mhz") or 10) * 1_000_000)
    transport = transport if isinstance(transport, dict) else {}
    inputs = transport.get("input_bit_map") if isinstance(transport.get("input_bit_map"), list) else []
    outputs = transport.get("output_bit_map") if isinstance(transport.get("output_bit_map"), list) else []
    fields = _register_fields(inputs, outputs)
    reg_valid, reg_we = fields["valid"], fields["write"]
    reg_addr, reg_wdata = fields["address"], fields["write_data"]
    reg_rdata, reg_ready = fields["read_data"], fields["ready"]
    has_register_api = all((reg_valid, reg_we, reg_addr, reg_wdata, reg_rdata, reg_ready))
    register_api = ""
    if has_register_api:
        register_api = f'''
#define REG_VALID_LSB {int(reg_valid["lsb"])}
#define REG_WE_LSB {int(reg_we["lsb"])}
#define REG_ADDR_LSB {int(reg_addr["lsb"])}
#define REG_ADDR_WIDTH {int(reg_addr["width"])}
#define REG_WDATA_LSB {int(reg_wdata["lsb"])}
#define REG_WDATA_WIDTH {int(reg_wdata["width"])}
#define REG_RDATA_LSB {int(reg_rdata["lsb"])}
#define REG_RDATA_WIDTH {int(reg_rdata["width"])}
#define REG_READY_LSB {int(reg_ready["lsb"])}
#define RESPONSE_PADDING_BITS {frame_bytes * 8 - int(transport.get("serialized_output_bits") or 0)}

static void frame_set_u32(uint8_t *frame, unsigned lsb, unsigned width, uint32_t value) {{
    for (unsigned bit = 0; bit < width; ++bit) {{
        const unsigned physical = lsb + bit;
        const unsigned byte = CHIPLOOP_FRAME_BYTES - 1u - physical / 8u;
        const uint8_t mask = (uint8_t)(1u << (physical % 8u));
        if ((value >> bit) & 1u) frame[byte] |= mask; else frame[byte] &= (uint8_t)~mask;
    }}
}}

static uint32_t frame_get_u32(const uint8_t *frame, unsigned lsb, unsigned width) {{
    uint32_t value = 0;
    for (unsigned bit = 0; bit < width; ++bit) {{
        const unsigned physical = RESPONSE_PADDING_BITS + lsb + bit;
        const unsigned byte = CHIPLOOP_FRAME_BYTES - 1u - physical / 8u;
        value |= (uint32_t)((frame[byte] >> (physical % 8u)) & 1u) << bit;
    }}
    return value;
}}

static esp_err_t chiploop_complete_command(uint8_t *request, uint8_t *response) {{
    uint8_t idle[CHIPLOOP_FRAME_BYTES] = {{0}};
    ESP_RETURN_ON_ERROR(chiploop_fpga_exchange(request, response), "chiploop", "command frame");
    ESP_RETURN_ON_ERROR(chiploop_fpga_exchange(idle, response), "chiploop", "pipeline frame 1");
    return chiploop_fpga_exchange(idle, response);
}}

esp_err_t chiploop_register_write(uint32_t address, uint32_t value) {{
    uint8_t request[CHIPLOOP_FRAME_BYTES] = {{0}}, response[CHIPLOOP_FRAME_BYTES] = {{0}};
    frame_set_u32(request, REG_VALID_LSB, 1, 1);
    frame_set_u32(request, REG_WE_LSB, 1, 1);
    frame_set_u32(request, REG_ADDR_LSB, REG_ADDR_WIDTH, address);
    frame_set_u32(request, REG_WDATA_LSB, REG_WDATA_WIDTH, value);
    ESP_RETURN_ON_ERROR(chiploop_complete_command(request, response), "chiploop", "register write");
    ESP_RETURN_ON_FALSE(frame_get_u32(response, REG_READY_LSB, 1), ESP_ERR_INVALID_RESPONSE, "chiploop", "register not ready");
    return ESP_OK;
}}

esp_err_t chiploop_register_read(uint32_t address, uint32_t *value) {{
    ESP_RETURN_ON_FALSE(value, ESP_ERR_INVALID_ARG, "chiploop", "read destination");
    uint8_t request[CHIPLOOP_FRAME_BYTES] = {{0}}, response[CHIPLOOP_FRAME_BYTES] = {{0}};
    frame_set_u32(request, REG_VALID_LSB, 1, 1);
    frame_set_u32(request, REG_ADDR_LSB, REG_ADDR_WIDTH, address);
    ESP_RETURN_ON_ERROR(chiploop_complete_command(request, response), "chiploop", "register read");
    ESP_RETURN_ON_FALSE(frame_get_u32(response, REG_READY_LSB, 1), ESP_ERR_INVALID_RESPONSE, "chiploop", "register not ready");
    *value = frame_get_u32(response, REG_RDATA_LSB, REG_RDATA_WIDTH);
    return ESP_OK;
}}
'''
    return f'''#include <stdint.h>
#include <string.h>
#include "driver/spi_master.h"
#include "esp_check.h"
#include "esp_heap_caps.h"
#include "esp_rom_sys.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"

#define CHIPLOOP_SPI_HOST SPI2_HOST
#define CHIPLOOP_PIN_SCLK {int(gpio.get("sclk", 14))}
#define CHIPLOOP_PIN_MOSI {int(gpio.get("mosi", 15))}
#define CHIPLOOP_PIN_MISO {int(gpio.get("miso", 2))}
#define CHIPLOOP_PIN_CS {int(gpio.get("cs_n", 13))}
#define CHIPLOOP_FRAME_BYTES {frame_bytes}
#define CHIPLOOP_SPI_CLOCK_HZ {clock_hz}

static spi_device_handle_t chiploop_fpga;
static uint8_t *chiploop_tx_dma;
static uint8_t *chiploop_rx_dma;
static SemaphoreHandle_t chiploop_exchange_lock;

static esp_err_t chiploop_fpga_init(void) {{
    const spi_bus_config_t bus = {{
        .mosi_io_num = CHIPLOOP_PIN_MOSI, .miso_io_num = CHIPLOOP_PIN_MISO,
        .sclk_io_num = CHIPLOOP_PIN_SCLK, .quadwp_io_num = -1,
        .quadhd_io_num = -1, .max_transfer_sz = CHIPLOOP_FRAME_BYTES,
    }};
    const spi_device_interface_config_t device = {{
        .clock_speed_hz = CHIPLOOP_SPI_CLOCK_HZ, .mode = 0,
        .spics_io_num = CHIPLOOP_PIN_CS, .queue_size = 1,
    }};
    ESP_RETURN_ON_ERROR(spi_bus_initialize(CHIPLOOP_SPI_HOST, &bus, SPI_DMA_CH_AUTO), "chiploop", "spi bus");
    ESP_RETURN_ON_ERROR(spi_bus_add_device(CHIPLOOP_SPI_HOST, &device, &chiploop_fpga), "chiploop", "spi device");
    chiploop_tx_dma = heap_caps_calloc(CHIPLOOP_FRAME_BYTES, 1, MALLOC_CAP_DMA);
    chiploop_rx_dma = heap_caps_calloc(CHIPLOOP_FRAME_BYTES, 1, MALLOC_CAP_DMA);
    ESP_RETURN_ON_FALSE(chiploop_tx_dma && chiploop_rx_dma, ESP_ERR_NO_MEM, "chiploop", "DMA buffers");
    chiploop_exchange_lock = xSemaphoreCreateMutex();
    ESP_RETURN_ON_FALSE(chiploop_exchange_lock, ESP_ERR_NO_MEM, "chiploop", "exchange mutex");
    return ESP_OK;
}}

esp_err_t chiploop_fpga_exchange(const uint8_t *request, uint8_t *response) {{
    ESP_RETURN_ON_FALSE(request && response && chiploop_tx_dma && chiploop_rx_dma && chiploop_exchange_lock, ESP_ERR_INVALID_STATE, "chiploop", "exchange state");
    ESP_RETURN_ON_FALSE(xSemaphoreTake(chiploop_exchange_lock, portMAX_DELAY) == pdTRUE, ESP_ERR_TIMEOUT, "chiploop", "exchange mutex");
    memcpy(chiploop_tx_dma, request, CHIPLOOP_FRAME_BYTES);
    memset(chiploop_rx_dma, 0, CHIPLOOP_FRAME_BYTES);
    spi_transaction_t transaction;
    memset(&transaction, 0, sizeof(transaction));
    transaction.length = CHIPLOOP_FRAME_BYTES * 8;
    transaction.tx_buffer = chiploop_tx_dma;
    transaction.rx_buffer = chiploop_rx_dma;
    esp_err_t status = spi_device_transmit(chiploop_fpga, &transaction);
    // Allow the FPGA core-clock synchronizer to commit the mailbox before a
    // subsequent transaction begins. Response N is visible in frame N+2.
    esp_rom_delay_us(1);
    if (status == ESP_OK) memcpy(response, chiploop_rx_dma, CHIPLOOP_FRAME_BYTES);
    xSemaphoreGive(chiploop_exchange_lock);
    return status;
}}
{register_api}

void app_main(void) {{ ESP_ERROR_CHECK(chiploop_fpga_init()); }}
'''


def run_esp_idf_build(state: dict) -> dict:
    ensure_workflow_dir(state)
    workflow_dir = os.path.abspath(str(state.get("workflow_dir") or os.getcwd()))
    refinement = state.get("target_refinement") if isinstance(state.get("target_refinement"), dict) else {}
    host = refinement.get("compute_host") if isinstance(refinement.get("compute_host"), dict) else {}
    interface = host.get("fabric_interface") if isinstance(host.get("fabric_interface"), dict) else {}
    if interface.get("protocol") != "spi_register":
        raise RuntimeError("ESP-IDF build requires the qualified spi_register fabric interface contract.")
    transport = refinement.get("transport_contract") if isinstance(refinement.get("transport_contract"), dict) else {}
    input_bits = int(transport.get("serialized_input_bits") or 0)
    output_bits = int(transport.get("serialized_output_bits") or 0)
    if input_bits <= 0 or output_bits <= 0:
        raise RuntimeError("ESP-IDF build requires serialized widths from the verified FPGA wrapper contract.")
    frame_bytes = (max(input_bits, output_bits) + 7) // 8
    frame_bits = frame_bytes * 8
    if int(transport.get("frame_bits") or 0) != frame_bits or int(transport.get("frame_bytes") or 0) != frame_bytes:
        raise RuntimeError("ESP-IDF build rejected inconsistent byte-aligned FPGA frame metadata.")
    if int(transport.get("response_latency_frames") or 0) != 2:
        raise RuntimeError("ESP-IDF build requires the qualified two-frame bundled-data response contract.")
    fields = _register_fields(transport.get("input_bit_map") or [], transport.get("output_bit_map") or [])
    if not all(fields.values()):
        missing = [role for role, field in fields.items() if not field]
        raise RuntimeError(
            "ESP32 integration requires a complete semantic register transport map; missing roles: "
            + ", ".join(missing)
        )
    selected_ports = {str(field.get("port") or "").lower() for field in fields.values()}
    for item in [*(transport.get("input_bit_map") or []), *(transport.get("output_bit_map") or [])]:
        if isinstance(item, dict) and str(item.get("port") or "").lower() in selected_ports and int(item.get("width") or 0) > 32:
            raise RuntimeError("ESP-IDF register API currently supports register fields up to 32 bits.")

    write_artifact(state, "firmware/esp_idf/CMakeLists.txt", 'cmake_minimum_required(VERSION 3.16)\ninclude($ENV{IDF_PATH}/tools/cmake/project.cmake)\nproject(chiploop_fpga_host)\n', key="esp_idf_cmake")
    write_artifact(state, "firmware/esp_idf/main/CMakeLists.txt", 'idf_component_register(SRCS "main.c" INCLUDE_DIRS ".")\n', key="esp_idf_main_cmake")
    write_artifact(state, "firmware/esp_idf/main/main.c", _main_source(interface, frame_bytes, transport), key="esp_idf_main")
    contract = {
        "schema": "chiploop.esp_idf.fpga_host.v1", "status": "generated", "idf_target": "esp32",
        "frame_bits": frame_bits, "frame_bytes": frame_bytes,
        "command_leading_padding_bits": frame_bits - input_bits,
        "response_trailing_padding_bits": frame_bits - output_bits,
        "response_latency_frames": 2,
        "minimum_interframe_delay_us": 1,
        "serialized_input_bits": input_bits,
        "serialized_output_bits": output_bits,
        "input_bit_map": transport.get("input_bit_map") or [],
        "output_bit_map": transport.get("output_bit_map") or [],
        "fabric_interface": interface,
    }
    write_artifact(state, "firmware/esp_idf/interface_contract.json", json.dumps(contract, indent=2), key="esp_idf_contract")

    idf = _idf_tool()
    if not idf:
        raise RuntimeError("ESP-IDF idf.py is not installed; no alternate compiler is permitted for this target.")
    project_dir = os.path.join(workflow_dir, "firmware", "esp_idf")
    idf_path = str(os.getenv("IDF_PATH") or "").strip()
    if idf_path and os.path.isfile(os.path.join(idf_path, "export.sh")):
        command = '. "$IDF_PATH/export.sh" >/dev/null && "$IDF_PATH/tools/idf.py"'
        completed = subprocess.run(["bash", "-lc", command + " set-target esp32"], cwd=project_dir, capture_output=True, text=True, check=False)
        build_command = ["bash", "-lc", command + " build"]
    else:
        completed = subprocess.run([idf, "set-target", "esp32"], cwd=project_dir, capture_output=True, text=True, check=False)
        build_command = [idf, "build"]
    if completed.returncode == 0:
        completed = subprocess.run(build_command, cwd=project_dir, capture_output=True, text=True, check=False)
    write_artifact(state, "firmware/debug/esp_idf_build.log", (completed.stdout or "") + "\n" + (completed.stderr or ""), key="esp_idf_build_log")
    elf_path = os.path.join(project_dir, "build", "chiploop_fpga_host.elf")
    if completed.returncode != 0 or not os.path.isfile(elf_path):
        raise RuntimeError("ESP-IDF compilation failed for the ULX3S onboard ESP32 firmware.")
    summary = {**contract, "status": "built", "elf_path": elf_path, "tool": idf}
    write_artifact(state, "firmware/debug/elf_build_result.json", json.dumps(summary, indent=2), key="elf_build_result")
    state.setdefault("firmware", {})["elf_build"] = summary
    state["firmware_elf_path"] = elf_path
    return state
