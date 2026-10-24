// SPDX-License-Identifier: MIT
// What a board supplies to the vendor-neutral Metal plugin (NativeVulkanMetalPlugin.mm): the plugin itself only knows the Metal
// frontend (NativeBridgeMetalFrontend.mm), the metal2vulkan bridge and the display hand-off protocol (GpuScanoutClient.h); how the
// board's Vulkan driver reaches its kernel driver, where progress goes and how the hand-off is reached are the board's. A board
// source that defines these four functions is compiled into the plugin by scripts/build-vulkan-metal-plugin.py --board-source.
// The Adreno plugin (NativeAdrenoMetalPlugin.mm) does not use this header.
#pragma once
#include <stdint.h>
#include "GpuScanoutClient.h"

extern "C" {
// Once per process, before the Vulkan driver is opened: the driver's environment (its kernel service, open type and the like).
// port is the accelerator port the original discovery passed to initWithAcceleratorPort:; the discovery releases it when the
// plugin returns, so a board that keeps using it retains it here.
void native_metal_board_prepare(unsigned port);
// A progress marker of the plugin (the values are listed in NativeVulkanMetalPlugin.mm). May do nothing.
void native_metal_board_mark(uint32_t value);
// One line of text (at most 63 characters are kept) for the board's log. May do nothing.
void native_metal_board_text(const char *line);
// The display hand-off of the render server (boot argument sheng-fb-gpu=1): fills transport and returns true when the board offers
// one; false keeps the display driver's own copy.
bool native_metal_board_scanout(gpuscanout::Transport *transport);
}
