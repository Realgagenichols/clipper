<div align="center">

# 🐬 clipper

**Drive your Flipper Zero with natural language.**

clipper is an [MCP](https://modelcontextprotocol.io) server that exposes a [Flipper Zero](https://flipperzero.one) over USB serial as native tools for Claude Code (and any other MCP client) — read GPIO, capture & replay Sub-GHz, scan NFC/RFID, emulate cards, browse the device filesystem, recover MIFARE Classic keys, and more. No model API credits required.

![Python](https://img.shields.io/badge/python-3.12%2B-3776AB?logo=python&logoColor=white)
![License: MIT](https://img.shields.io/badge/license-MIT-22c55e)
![Protocol: MCP](https://img.shields.io/badge/protocol-MCP-7c3aed)
![Firmware: Momentum](https://img.shields.io/badge/firmware-Momentum-f97316)
![Tools: 32](https://img.shields.io/badge/tools-32-0ea5e9)

</div>

---

## ✨ What it looks like

Once it's connected, you just *ask* — the model picks the right tool:

> 💬 *"Is my Flipper connected, and what's the battery?"*
> → `flipper_state` → *Connected — Pelima on mntm-012, battery 80%.*

> 💬 *"Capture Sub-GHz on 433.92 MHz for 10 seconds and tell me how many packets you saw."*
> → `flipper_subghz_rx` → *Captured 3 packets in 10s.*

> 💬 *"List the apps on the device, then open the NFC app."*
> → `flipper_loader_list` → `flipper_loader_open`

Every capability is a discrete, schema-validated MCP tool, so you stay in plain English.

## 🚀 Quickstart

> **Requirements:** Python 3.12+, [uv](https://docs.astral.sh/uv/), and a Flipper Zero on **[Momentum](https://momentum-fw.dev/) firmware** connected over USB.

```bash
# 1. Install (handles the macOS uv/.pth quirks for you)
git clone https://github.com/Realgagenichols/clipper.git
cd clipper
make sync

# 2. Register with Claude Code
claude mcp add clipper -- /absolute/path/to/clipper/scripts/clipper-mcp-stdio.sh
```

Then start a Claude Code session and ask it to *"use flipper_state to check the battery."* That's it.

To enable the transmit / emulate / state-changing tools, add the safety gate (see [Safety & responsible use](#-safety--responsible-use)):

```bash
claude mcp add clipper -- env CLIPPER_SAFETY=1 /absolute/path/to/clipper/scripts/clipper-mcp-stdio.sh
```

The server starts fine with no device attached — only tools that touch the serial port report a disconnect, and it auto-reconnects when you plug back in.

## 🧰 Tools

32 tools, grouped by subsystem. **⚡ = emissive** (transmits RF/IR/USB) · **🔒 = state-changing**. Both are blocked unless the [safety gate](#-safety--responsible-use) is on.

#### Device & diagnostics
| Tool | Description | |
|------|-------------|:--:|
| `flipper_state` | Connection status, name, firmware, battery | |
| `flipper_uptime` | Device uptime | |
| `flipper_datetime` | Read the device clock (RTC) | |
| `flipper_diagnostics` | Heap + task diagnostics | |
| `flipper_power` | Power off / reboot / reboot2dfu | 🔒 |
| `flipper_power_otg` | Toggle external 5V OTG output | 🔒 |

#### GPIO
| Tool | Description | |
|------|-------------|:--:|
| `flipper_gpio_read` | Read a GPIO pin level | |
| `flipper_gpio_write` | Write a GPIO pin level | |

#### Infrared
| Tool | Description | |
|------|-------------|:--:|
| `flipper_ir_rx` | Receive IR signals (timeout-based) | |
| `flipper_ir_tx` | Transmit an IR signal | ⚡ |

#### Sub-GHz
| Tool | Description | |
|------|-------------|:--:|
| `flipper_subghz_rx` | Capture Sub-GHz packets (timeout-based) | |
| `flipper_subghz_tx` | Transmit a Sub-GHz signal | ⚡ |
| `flipper_subghz_tx_from_file` | Replay a saved `.sub` file | ⚡ |

#### NFC · RFID · iButton
| Tool | Description | |
|------|-------------|:--:|
| `flipper_nfc_read` | Read an NFC tag | |
| `flipper_rfid_read` | Read a 125 kHz RFID card | |
| `flipper_rfid_emulate` | Emulate a 125 kHz RFID card (timeout-based) | ⚡ |
| `flipper_ibutton_emulate` | Emulate an iButton key (timeout-based) | ⚡ |
| `flipper_mfkey_crack` | Recover MIFARE Classic keys (mfkey32v2) | 🔒 |

#### USB / HID
| Tool | Description | |
|------|-------------|:--:|
| `flipper_badusb_run` | Run a DuckyScript payload | ⚡ |

#### Storage
| Tool | Description | |
|------|-------------|:--:|
| `flipper_storage_info` | Filesystem space info | |
| `flipper_storage_list` | List a directory | |
| `flipper_storage_stat` | Stat a path | |
| `flipper_storage_md5sum` | MD5 hash of a file on the device | |
| `flipper_storage_read` | Read a file (optionally download to host) | |
| `flipper_storage_write` | Write a file to the device | 🔒 |

#### Apps & on-device feedback
| Tool | Description | |
|------|-------------|:--:|
| `flipper_loader_open` | Open an app on the device | |
| `flipper_loader_list` | List available apps | |
| `flipper_loader_info` | Report the running app | |
| `flipper_loader_close` | Close the running app | |
| `flipper_led_set` | Set the on-device LED state | |
| `flipper_led_color` | Set the on-device LED color | |
| `flipper_vibro_set` | Toggle the vibration motor | |

## 🔐 Safety & responsible use

Tools that transmit (IR/Sub-GHz TX, file replay, BadUSB), emulate a credential (RFID/iButton), or change device state (storage write, key recovery, power off/reboot, 5V OTG) are **blocked by default**. Unlock them with:

```bash
CLIPPER_SAFETY=1
```

- **Audit trail.** Every gated invocation — allowed *or* denied — is appended to `~/.clipper/audit.log` (override with `CLIPPER_AUDIT_PATH`). It records the action, transport, outcome, and parameters, and **never** logs secrets (credential data like RFID/iButton keys is redacted).
- **Frequency allow-list.** Sub-GHz *transmits* are restricted to an allow-list (override with `CLIPPER_SGHZ_ALLOWED_MHZ`). Receiving is unrestricted.
- **Use it lawfully.** clipper can transmit RF and emulate access credentials. Only use it on hardware, signals, and credentials you own or are explicitly authorized to test. You are responsible for complying with local radio and computer-misuse laws.

## ⚙️ Configuration

All configuration is via environment variables:

| Variable | Default | Purpose |
|----------|---------|---------|
| `CLIPPER_SAFETY` | unset | Set to `1` to unlock emissive / state-changing tools |
| `CLIPPER_FLIPPER_PORT` | auto-detect | Serial port to use (e.g. `/dev/cu.usbmodemflip_Xxxx`) |
| `CLIPPER_SGHZ_ALLOWED_MHZ` | built-in list | Comma-separated allowed Sub-GHz TX frequencies (MHz) |
| `CLIPPER_DOWNLOAD_DIR` | `~/.clipper/flipper-files` | Where `flipper_storage_read` downloads land |
| `CLIPPER_AUDIT_PATH` | `~/.clipper/audit.log` | Audit log location |
| `CLIPPER_LOG_LEVEL` | `INFO` | Log level (logs go to stderr; stdout is the JSON-RPC stream) |

> `CLIPPER_ALLOW_EMIT=1` is honored as a deprecated alias for `CLIPPER_SAFETY=1`.

## 🛠️ How it works

```
Claude / MCP client  ──stdio (JSON-RPC)──▶  clipper MCP server
                                                  │
                                          action registry  ── every action = one MCP tool
                                                  │
                                   FlipperConnection (single locked serial port)
                                          │                         │
                                    text CLI commands         binary RPC (protobuf)
                                                  ▼
                                            Flipper Zero
```

A single `FlipperConnection` owns the serial port behind an async lock, so concurrent tool calls are serialized cleanly. Most tools shell out to the Flipper's text CLI; binary-safe file transfer uses the RPC protobuf protocol. The connection drains the line to quiescence between operations and auto-recovers from device reboots / re-enumeration.

## 🧑‍💻 Development

```bash
make test    # run the full test suite
make lint    # ruff

# a single test file
UV_NO_EDITABLE=0 uv run pytest tests/test_mcp.py -v
```

Contributions welcome — please keep tools small and schema-validated, add tests for new behavior (TDD), and run `make test && make lint` before opening a PR.

## 🙏 Acknowledgements

- The [Flipper Zero](https://flipperzero.one) team and the [Momentum firmware](https://momentum-fw.dev/) project.
- The [Model Context Protocol](https://modelcontextprotocol.io).
- MIFARE Classic key recovery ports the Crypto1 / mfkey32v2 algorithm from the Flipper firmware and proxmark3 lineage.

## 📄 License

[MIT](LICENSE) © Gage Nichols
