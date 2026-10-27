// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef WEECHAT_JS_MANAGED_H
#define WEECHAT_JS_MANAGED_H

bool weechat_js_managed_init();
void weechat_js_managed_end();
bool weechat_js_managed_path(const char *filename);
char *weechat_js_managed_read_trusted(const char *filename, bool *handled);

#endif
