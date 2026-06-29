#!/usr/bin/env python3
# Save ArcGIS Map Viewer HTML to snapshot/<date>.html for scrape_trees.py.
# Run from outside the repo (e.g. /tmp) so this folder doesn't shadow pip selenium.

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
from pathlib import Path

from selenium import webdriver
from selenium.common.exceptions import JavascriptException, NoSuchWindowException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.actions.action_builder import ActionBuilder
from selenium.webdriver.common.actions.pointer_input import PointerInput
from selenium.webdriver.common.actions.wheel_input import ScrollOrigin

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

MAP_VIEWER_URL = (
    "https://geo.maps.arcgis.com/apps/mapviewer/index.html"
    "?layers=565c6a9705724f0baead0c118aee9c92"
)

# Output folder (relative to the repo root, matching scrape_trees.py).
OUTPUT_DIR = Path(__file__).resolve().parent.parent / "snapshot"

DEFAULT_START = dt.date(2022, 1, 1)
DEFAULT_END = dt.date(2026, 1, 15)
DEFAULT_STEP_DAYS = 7

# How long (s) to wait for the app shell / map to become interactive.
PAGE_LOAD_TIMEOUT = 120
# If the page stops growing with no time slider, stop waiting after this many seconds.
STALL_TIMEOUT = 30
# Minimum HTML size for a saved snapshot (after Time panel is configured).
MIN_HTML_BYTES = 200_000
# Minimum HTML size to consider the app shell loaded (much smaller than a full snapshot).
MIN_SHELL_HTML_BYTES = 50_000
# Extra settle time (s) after setting the date before saving the DOM, so the
# tiles / time slider have a chance to re-render for the new window.
RENDER_SETTLE_SECONDS = 5.0

LAYER_NAME = "Land Cover Willamette Valley Hardwood Model"


# --------------------------------------------------------------------------- #
# Injected JavaScript helpers
# --------------------------------------------------------------------------- #

# Walks the whole document including every open shadow root and returns all
# elements whose tag name matches one of `tags` (case-insensitive).
_DEEP_QUERY_JS = """
const tags = arguments[0].map(t => t.toLowerCase());
const out = [];
const seen = new Set();
function visit(root) {
  if (!root || seen.has(root)) return;
  seen.add(root);
  const all = root.querySelectorAll('*');
  for (const el of all) {
    if (tags.includes(el.tagName.toLowerCase())) out.push(el);
    if (el.shadowRoot) visit(el.shadowRoot);
  }
}
visit(document);
return out;
"""

# Sets a Calcite date/time window. Finds date pickers (and time pickers) across
# all shadow roots, assigns start to the first and end to the second, and fires
# the change events Calcite/the app listen for. Returns a small report object so
# the Python side can verify the controls were actually found.
_SET_TIME_JS = """
const startDate = arguments[0];   // 'YYYY-MM-DD'
const endDate = arguments[1];     // 'YYYY-MM-DD'
const startTime = arguments[2];   // 'HH:MM:SS'
const endTime = arguments[3];     // 'HH:MM:SS'

function toUsDate(iso) {
  const [y, m, d] = iso.split('-');
  return `${parseInt(m, 10)}/${parseInt(d, 10)}/${y}`;
}

function deep(tag) {
  const out = [];
  const seen = new Set();
  (function visit(root) {
    if (!root || seen.has(root)) return;
    seen.add(root);
    for (const el of root.querySelectorAll(tag)) out.push(el);
    for (const el of root.querySelectorAll('*')) if (el.shadowRoot) visit(el.shadowRoot);
  })(document);
  return out;
}

function timeWindowPickers() {
  const out = [];
  const seen = new Set();
  (function visit(root) {
    if (!root || seen.has(root)) return;
    seen.add(root);
    if (!root.querySelectorAll) return;
    for (const wrap of root.querySelectorAll('.date-time-wrapper')) {
      for (const picker of wrap.querySelectorAll('calcite-input-date-picker')) {
        out.push(picker);
      }
    }
    for (const el of root.querySelectorAll('*')) if (el.shadowRoot) visit(el.shadowRoot);
  })(document);
  return out;
}

function timeWindowTimePickers() {
  const out = [];
  const seen = new Set();
  (function visit(root) {
    if (!root || seen.has(root)) return;
    seen.add(root);
    if (!root.querySelectorAll) return;
    for (const wrap of root.querySelectorAll('.date-time-wrapper')) {
      for (const picker of wrap.querySelectorAll('calcite-input-time-picker')) {
        out.push(picker);
      }
    }
    for (const el of root.querySelectorAll('*')) if (el.shadowRoot) visit(el.shadowRoot);
  })(document);
  return out;
}

function fire(el, names) {
  for (const n of names) {
    el.dispatchEvent(new CustomEvent(n, { bubbles: true, composed: true }));
  }
  el.dispatchEvent(new Event('input', { bubbles: true, composed: true }));
  el.dispatchEvent(new Event('change', { bubbles: true, composed: true }));
}

function setDatePicker(picker, iso) {
  if (!picker) return;
  picker.value = iso;
  fire(picker, ['calciteInputDatePickerChange']);
  const input = picker.shadowRoot &&
    picker.shadowRoot.querySelector('calcite-input-text, input');
  if (input) {
    if (input.value !== undefined) input.value = toUsDate(iso);
    fire(input, ['calciteInputTextInput', 'calciteInputTextChange']);
  }
}

let datePickers = timeWindowPickers();
let timePickers = timeWindowTimePickers();
if (datePickers.length < 2) datePickers = deep('calcite-input-date-picker').slice(0, 2);
if (timePickers.length < 2) timePickers = deep('calcite-input-time-picker').slice(0, 2);

if (datePickers.length >= 2) {
  setDatePicker(datePickers[0], startDate);
  setDatePicker(datePickers[1], endDate);
}
if (timePickers.length >= 2) {
  timePickers[0].value = startTime;
  fire(timePickers[0], ['calciteInputTimePickerChange']);
  timePickers[1].value = endTime;
  fire(timePickers[1], ['calciteInputTimePickerChange']);
}

return {
  datePickers: datePickers.length,
  timePickers: timePickers.length,
  startSet: datePickers[0] ? datePickers[0].value : null,
  endSet: datePickers[1] ? datePickers[1].value : null,
};
"""

# Builds a full inventory of the live page across every open shadow root:
#   - tagCounts: how many of each custom element (tag containing '-') exist
#   - controls : every clickable control (button / calcite-action / [role=button]
#                / list item) with its accessible name, to identify what to click
#   - timeish  : any element whose tag or accessible name hints at date/time
# Used by --debug to map out the exact navigation needed to open the Time panel.
_INVENTORY_JS = """
const tagCounts = {};
const controls = [];
const timeish = [];
const seen = new Set();

function accName(el) {
  return (el.getAttribute('aria-label') || el.getAttribute('title') ||
          (el.textContent || '').trim()).slice(0, 60);
}

(function visit(root) {
  if (!root || seen.has(root)) return;
  seen.add(root);
  if (!root.querySelectorAll) return;
  for (const el of root.querySelectorAll('*')) {
    const tag = el.tagName.toLowerCase();
    if (tag.includes('-')) tagCounts[tag] = (tagCounts[tag] || 0) + 1;

    const role = el.getAttribute('role');
    const clickable = tag === 'button' || tag === 'calcite-action' ||
      tag === 'calcite-list-item' || tag === 'calcite-tab-title' ||
      role === 'button' || role === 'listitem';
    if (clickable) {
      const name = accName(el);
      if (name) controls.push({ tag, name, role: role || null });
    }

    const name = accName(el).toLowerCase();
    if (tag.includes('date') || tag.includes('time') || tag.includes('slider') ||
        name.includes('time') || name.includes('date')) {
      timeish.push({ tag, name: accName(el), value: el.value ?? null });
    }
    if (el.shadowRoot) visit(el.shadowRoot);
  }
})(document);

return { tagCounts, controls, timeish };
"""

# Reports whether the map, layer list, and time slider have finished rendering.
_READINESS_JS = """
const layerName = arguments[0];
const seen = new Set();
let timeSlider = 0;
let datePickers = 0;
let layerFound = false;
let mapView = 0;
let layerPropertiesBtn = false;

function visit(root) {
  if (!root || seen.has(root)) return;
  seen.add(root);
  if (!root.querySelectorAll) return;
  for (const el of root.querySelectorAll('*')) {
    const tag = el.tagName.toLowerCase();
    const cls = (el.className && el.className.toString) ? el.className.toString() : '';
    if (cls.includes('esri-time-slider')) timeSlider += 1;
    if (cls.includes('esri-view-root') || cls.includes('esri-view-surface')) mapView += 1;
    if (tag === 'arcgis-map') mapView += 1;
    if (tag === 'calcite-action' && el.id === 'layerproperties') layerPropertiesBtn = true;
    if (tag === 'calcite-input-date-picker') datePickers += 1;
    const text = (el.textContent || '').trim();
    if (!layerFound && text.includes(layerName)) layerFound = true;
    if (el.shadowRoot) visit(el.shadowRoot);
  }
}
visit(document);
const shellReady = !!document.querySelector('.calcite-shell-container.ready');
return {
  timeSlider,
  datePickers,
  layerFound,
  mapView,
  shellReady,
  layerPropertiesBtn,
  htmlLen: document.documentElement.outerHTML.length,
};
"""

# Clicks the first element whose accessible name matches `label`.
_CLICK_BY_LABEL_JS = """
const label = arguments[0];
const exact = arguments[1];
const seen = new Set();

function matches(name) {
  if (!name) return false;
  const n = name.trim().toLowerCase();
  const t = label.trim().toLowerCase();
  return exact ? n === t : n.includes(t);
}

function visit(root) {
  if (!root || seen.has(root)) return null;
  seen.add(root);
  if (!root.querySelectorAll) return null;
  const selectors = 'calcite-action, calcite-list-item, button, [role="button"]';
  for (const el of root.querySelectorAll(selectors)) {
    const name = el.getAttribute('aria-label') || el.getAttribute('title') ||
      el.getAttribute('text') || (el.textContent || '').trim();
    if (matches(name)) {
      const target = el.shadowRoot ? (el.shadowRoot.querySelector('button') || el) : el;
      target.click();
      return name;
    }
  }
  for (const el of root.querySelectorAll('*')) {
    if (el.shadowRoot) {
      const hit = visit(el.shadowRoot);
      if (hit) return hit;
    }
  }
  return null;
}
return visit(document);
"""

# Clicks an element whose visible text contains `needle`.
_CLICK_BY_TEXT_JS = """
const needle = arguments[0];
const seen = new Set();
function visit(root) {
  if (!root || seen.has(root)) return false;
  seen.add(root);
  if (!root.querySelectorAll) return false;
  for (const el of root.querySelectorAll('span, calcite-list-item, label, button, div')) {
    const text = (el.textContent || '').trim();
    if (text && text.includes(needle)) {
      (el.closest('calcite-list-item, button, [role="button"]') || el).click();
      return true;
    }
  }
  for (const el of root.querySelectorAll('*')) {
    if (el.shadowRoot && visit(el.shadowRoot)) return true;
  }
  return false;
}
return visit(document);
"""

# Clicks a calcite-action by its stable `id` (e.g. layerproperties, timeslider).
_CLICK_CALCITE_ACTION_ID_JS = """
const actionId = arguments[0];
const seen = new Set();

function visit(root) {
  if (!root || seen.has(root)) return false;
  seen.add(root);
  if (!root.querySelectorAll) return false;
  for (const el of root.querySelectorAll('calcite-action')) {
    if (el.id !== actionId) continue;
    el.scrollIntoView({ block: 'center', inline: 'center' });
    const btn = el.shadowRoot ? el.shadowRoot.querySelector('button') : null;
    if (btn) {
      btn.focus();
      btn.click();
    } else {
      el.click();
    }
    return true;
  }
  for (const el of root.querySelectorAll('*')) {
    if (el.shadowRoot && visit(el.shadowRoot)) return true;
  }
  return false;
}
return visit(document);
"""

# Fallback: click the layer-config action with a given Calcite icon name.
_CLICK_CALCITE_ACTION_ICON_JS = """
const icon = arguments[0];
const seen = new Set();

function visit(root) {
  if (!root || seen.has(root)) return false;
  seen.add(root);
  if (!root.querySelectorAll) return false;
  for (const el of root.querySelectorAll('calcite-action')) {
    if (el.getAttribute('icon') !== icon) continue;
    if (el.classList && el.classList.contains('js-action') === false && icon === 'clock') {
      // Time tab should be in the layer config action bar; skip unrelated clock icons.
      const title = (el.getAttribute('title') || '').toLowerCase();
      if (title && title !== 'time') continue;
    }
    el.scrollIntoView({ block: 'center', inline: 'center' });
    const btn = el.shadowRoot ? el.shadowRoot.querySelector('button') : null;
    if (btn) {
      btn.focus();
      btn.click();
    } else {
      el.click();
    }
    return el.id || icon;
  }
  for (const el of root.querySelectorAll('*')) {
    if (el.shadowRoot) {
      const hit = visit(el.shadowRoot);
      if (hit) return hit;
    }
  }
  return null;
}
return visit(document);
"""

# Clicks "Show properties" on the selected layer row in the Layers list.
_CLICK_LAYER_OPTIONS_JS = """
const seen = new Set();
function visit(root) {
  if (!root || seen.has(root)) return false;
  seen.add(root);
  if (!root.querySelectorAll) return false;
  for (const el of root.querySelectorAll('calcite-action[data-action-id="layer-options"]')) {
    const btn = el.shadowRoot ? el.shadowRoot.querySelector('button') : el;
    btn.scrollIntoView({ block: 'center', inline: 'center' });
    btn.click();
    return true;
  }
  for (const el of root.querySelectorAll('*')) {
    if (el.shadowRoot && visit(el.shadowRoot)) return true;
  }
  return false;
}
return visit(document);
"""

# Waits until the Properties side panel has rendered its flow / Time block.
_PROBE_PROPERTIES_PANEL_JS = """
function walk(visitFn) {
  const seen = new Set();
  const stack = [document.documentElement];
  while (stack.length) {
    const node = stack.pop();
    if (!node || seen.has(node)) continue;
    seen.add(node);
    if (node instanceof Element) {
      visitFn(node);
      if (node.shadowRoot) stack.push(node.shadowRoot);
      for (let i = node.children.length - 1; i >= 0; i--) stack.push(node.children[i]);
    } else if (node instanceof ShadowRoot) {
      for (let i = node.children.length - 1; i >= 0; i--) stack.push(node.children[i]);
    }
  }
}

let flow = false;
let timeBlock = false;
let timeslider = false;
let propertiesBtn = false;

walk((node) => {
  if (node.id === 'layer-properties-flow') flow = true;
  if (node.matches && node.matches('calcite-block.layerproperties-time-block')) timeBlock = true;
  if (node.id === 'timeslider') timeslider = true;
  if (node.id === 'layerproperties') propertiesBtn = true;
});

return { flow, timeBlock, timeslider, propertiesBtn,
         propertiesExpanded: (function() {
           let exp = false;
           walk((node) => {
             if (node.id === 'layerproperties') {
               exp = node.getAttribute('aria-expanded') === 'true';
             }
           });
           return exp;
         })()
       };
"""

# Returns {found, expanded} for a calcite-action toggle button.
_ACTION_STATE_JS = """
const actionId = arguments[0];
let found = false;
let expanded = false;
const seen = new Set();
const stack = [document.documentElement];
while (stack.length) {
  const node = stack.pop();
  if (!node || seen.has(node)) continue;
  seen.add(node);
  if (node instanceof Element) {
    if (node.id === actionId && node.tagName.toLowerCase() === 'calcite-action') {
      found = true;
      expanded = node.getAttribute('aria-expanded') === 'true';
      break;
    }
    if (node.shadowRoot) stack.push(node.shadowRoot);
    for (let i = node.children.length - 1; i >= 0; i--) stack.push(node.children[i]);
  } else if (node instanceof ShadowRoot) {
    for (let i = node.children.length - 1; i >= 0; i--) stack.push(node.children[i]);
  }
}
return { found, expanded };
"""

# Select the target layer in the Layers list (avoids clicking random matching text).
_SELECT_LAYER_JS = """
const layerName = arguments[0];
const seen = new Set();
function visit(root) {
  if (!root || seen.has(root)) return false;
  seen.add(root);
  if (!root.querySelectorAll) return false;
  for (const item of root.querySelectorAll('calcite-list-item')) {
    const text = (item.textContent || '').trim();
    if (text.includes(layerName)) {
      item.scrollIntoView({ block: 'center', inline: 'center' });
      item.click();
      return true;
    }
  }
  for (const el of root.querySelectorAll('*')) {
    if (el.shadowRoot && visit(el.shadowRoot)) return true;
  }
  return false;
}
return visit(document);
"""

# Expands the collapsible "Time" block in the layer Properties panel.
_EXPAND_TIME_BLOCK_JS = """
const seen = new Set();

function isExpanded(block) {
  return block.hasAttribute('expanded') && block.getAttribute('expanded') !== 'false';
}

function expandBlock(block) {
  const toggle = block.shadowRoot &&
    block.shadowRoot.querySelector('button.toggle, button#toggle, button[aria-controls="content"]');
  if (toggle) {
    toggle.scrollIntoView({ block: 'center', inline: 'center' });
    toggle.click();
    return true;
  }
  return false;
}

function visit(root) {
  if (!root || seen.has(root)) return null;
  seen.add(root);
  if (!root.querySelectorAll) return null;

  for (const block of root.querySelectorAll('calcite-block.layerproperties-time-block')) {
    if (isExpanded(block)) return 'already-expanded';
    return expandBlock(block) ? 'expanded' : 'toggle-not-found';
  }

  for (const block of root.querySelectorAll('calcite-block[collapsible]')) {
    const heading = block.shadowRoot && block.shadowRoot.querySelector('.heading');
    if (!heading || heading.textContent.trim() !== 'Time') continue;
    if (isExpanded(block)) return 'already-expanded';
    return expandBlock(block) ? 'expanded-fallback' : 'time-block-not-found';
  }

  for (const el of root.querySelectorAll('*')) {
    if (el.shadowRoot) {
      const hit = visit(el.shadowRoot);
      if (hit) return hit;
    }
  }
  return null;
}
return visit(document);
"""

# Shared helpers: scroll the Properties panel to the Time block and locate the
# "Time-based layer visibility" calcite-switch (including slotted light DOM).
_TIME_UI_JS = """
const mode = arguments[0];

function walkElements(visitFn) {
  const seen = new Set();
  const stack = [document.documentElement];
  const hits = [];
  while (stack.length) {
    const node = stack.pop();
    if (!node || seen.has(node)) continue;
    seen.add(node);
    if (node instanceof Element) {
      const hit = visitFn(node);
      if (hit) hits.push(hit);
      if (node.shadowRoot) stack.push(node.shadowRoot);
      for (let i = node.children.length - 1; i >= 0; i--) stack.push(node.children[i]);
    } else if (node instanceof ShadowRoot) {
      for (let i = node.children.length - 1; i >= 0; i--) stack.push(node.children[i]);
    }
  }
  return hits;
}

function switchFromHost(host) {
  if (!host || !host.querySelector) return null;
  const comp = host.querySelector('arcgis-map-config-time-extent-visibility');
  if (comp) {
    const sr = comp.shadowRoot;
    const sw = (sr && sr.querySelector('calcite-switch')) || comp.querySelector('calcite-switch');
    if (sw) return sw;
  }
  for (const sw of host.querySelectorAll('calcite-switch')) {
    const roleSwitch = sw.shadowRoot && sw.shadowRoot.querySelector('[role="switch"]');
    const label = ((roleSwitch && roleSwitch.getAttribute('aria-label')) || '').toLowerCase();
    if (label.includes('time-based layer visibility')) return sw;
  }
  return null;
}

function findTimeBlock() {
  let found = null;
  walkElements((node) => {
    if (found) return null;
    if (node.matches && node.matches('calcite-block.layerproperties-time-block')) {
      found = node;
    }
    return null;
  });
  if (found) return found;
  walkElements((node) => {
    if (found) return null;
    if (node.matches && node.matches('calcite-block[collapsible]')) {
      const heading = node.shadowRoot && node.shadowRoot.querySelector('.heading');
      if (heading && heading.textContent.trim() === 'Time') found = node;
    }
    return null;
  });
  return found;
}

function findVisibilitySwitch() {
  let found = null;
  walkElements((node) => {
    if (found) return null;
    if (node.matches && (
      node.matches('calcite-block.layerproperties-time-block') ||
      node.matches('arcgis-map-config-time-extent-visibility')
    )) {
      const sw = switchFromHost(node);
      if (sw) found = sw;
    }
    if (!found) {
      const sw = switchFromHost(node);
      if (sw) found = sw;
    }
    return null;
  });
  return found;
}

function switchIsOn(sw) {
  const roleSwitch = sw.shadowRoot && sw.shadowRoot.querySelector('[role="switch"]');
  return !!(
    sw.checked ||
    sw.hasAttribute('checked') ||
    (roleSwitch && roleSwitch.getAttribute('aria-checked') === 'true')
  );
}

function switchClickRect(sw) {
  const roleSwitch = sw.shadowRoot && sw.shadowRoot.querySelector('[role="switch"]');
  const track = sw.shadowRoot && sw.shadowRoot.querySelector('.track, .handle, .container');
  const el = track || roleSwitch || sw;
  const r = el.getBoundingClientRect();
  return {
    x: Math.round(r.left + r.width / 2),
    y: Math.round(r.top + r.height / 2),
    width: r.width,
    height: r.height,
    top: r.top,
    left: r.left,
  };
}

function findPropertiesPanelContent() {
  const scrollers = [];

  function addScroller(el, label) {
    if (!el) return;
    scrollers.push({ el, label });
  }

  walkElements((node) => {
    if (node.matches && node.matches('calcite-flow')) {
      const hasProps = node.querySelector('calcite-flow-item[heading="Properties"]')
        || node.id === 'layer-properties-flow';
      if (!hasProps) return null;
      for (const item of node.querySelectorAll('calcite-flow-item')) {
        const heading = item.getAttribute('heading') || '';
        if (heading && heading !== 'Properties') continue;
        const panel = item.shadowRoot && item.shadowRoot.querySelector('calcite-panel');
        if (!panel || !panel.shadowRoot) continue;
        addScroller(panel.shadowRoot.querySelector('div.content'), 'calcite-panel.content');
        addScroller(panel.shadowRoot.querySelector('.content-bottom'), 'calcite-panel.content-bottom');
        addScroller(panel.shadowRoot.querySelector('article.container'), 'calcite-panel.container');
      }
    }

    if (node.id === 'layer-properties-flow') {
      for (const item of node.querySelectorAll('calcite-flow-item')) {
        const panel = item.shadowRoot && item.shadowRoot.querySelector('calcite-panel');
        if (!panel || !panel.shadowRoot) continue;
        addScroller(panel.shadowRoot.querySelector('div.content'), 'calcite-panel.content');
      }
    }

    if (node.matches && node.matches('calcite-action#layerproperties')) {
      const panelId = node.getAttribute('aria-controls');
      const panel = panelId && document.getElementById(panelId);
      if (panel && panel.shadowRoot) {
        addScroller(panel.shadowRoot.querySelector('.content__body'), 'shell-panel.content__body');
        addScroller(panel.shadowRoot.querySelector('.content-container .content'), 'shell-panel.content');
      }
    }
    return null;
  });

  return scrollers;
}

function scrollPropertiesPanel() {
  const scrollers = findPropertiesPanelContent();
  let scrolled = 0;
  for (const { el, label } of scrollers) {
    if (!el || el.scrollHeight <= el.clientHeight + 2) continue;
    el.scrollTop = el.scrollHeight;
    scrolled += 1;
  }
  return { scrollerCount: scrollers.length, scrolledCount: scrolled };
}

function scrollToTimeSection() {
  const block = findTimeBlock();
  const sw = findVisibilitySwitch();

  const panelScroll = scrollPropertiesPanel();

  function scrollAncestors(el) {
    let node = el;
    const seen = new Set();
    while (node && !seen.has(node)) {
      seen.add(node);
      if (node.scrollHeight > node.clientHeight + 2) {
        node.scrollTop = node.scrollHeight;
      }
      if (node.parentElement) {
        node = node.parentElement;
      } else {
        const root = node.getRootNode();
        node = root && root.host ? root.host : null;
      }
    }
  }

  if (block) {
    block.scrollIntoView({ block: 'end', inline: 'nearest', behavior: 'auto' });
    scrollAncestors(block);
  }
  if (sw) {
    sw.scrollIntoView({ block: 'center', inline: 'nearest', behavior: 'auto' });
  }

  // Second pass after layout settles.
  scrollPropertiesPanel();

  const scrollers = findPropertiesPanelContent();
  let center = null;
  for (const { el } of scrollers) {
    if (!el || el.scrollHeight <= el.clientHeight + 2) continue;
    el.scrollTop = el.scrollHeight;
    const r = el.getBoundingClientRect();
    center = { x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2) };
  }

  const rect = sw ? switchClickRect(sw) : null;
  return {
    blockFound: !!block,
    switchFound: !!sw,
    panelScroll,
    scrollerTargets: scrollers.map(s => s.label),
    scrollCenter: center,
    switchRect: rect,
  };
}

function scrollerInfo() {
  const scrollers = findPropertiesPanelContent();
  let center = null;
  for (const { el } of scrollers) {
    if (!el) continue;
    const r = el.getBoundingClientRect();
    if (r.width > 0 && r.height > 0) {
      center = { x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2) };
      break;
    }
  }
  return { center, scrollerTargets: scrollers.map(s => s.label) };
}

function switchInfo() {
  const sw = findVisibilitySwitch();
  if (!sw) return { found: false, checked: false, rect: null };
  return { found: true, checked: switchIsOn(sw), rect: switchClickRect(sw) };
}

if (mode === 'scroll') {
  return scrollToTimeSection();
}
if (mode === 'scroller-info') {
  return scrollerInfo();
}
if (mode === 'info') {
  return switchInfo();
}
return switchInfo();
"""

# Legacy inline toggler kept as fallback after CDP click attempts.
_ENABLE_TIME_VISIBILITY_JS = """
function switchState(sw) {
  const roleSwitch = sw.shadowRoot && sw.shadowRoot.querySelector('[role="switch"]');
  const on = !!(
    sw.checked ||
    sw.hasAttribute('checked') ||
    (roleSwitch && roleSwitch.getAttribute('aria-checked') === 'true')
  );
  return { on, roleSwitch };
}

function fireSwitchChange(sw, checked) {
  sw.checked = checked;
  if (checked) sw.setAttribute('checked', '');
  else sw.removeAttribute('checked');
  const roleSwitch = sw.shadowRoot && sw.shadowRoot.querySelector('[role="switch"]');
  if (roleSwitch) roleSwitch.setAttribute('aria-checked', checked ? 'true' : 'false');
  sw.dispatchEvent(new CustomEvent('calciteSwitchChange', {
    bubbles: true,
    composed: true,
    detail: { checked },
  }));
}

function clickSwitch(sw) {
  const roleSwitch = sw.shadowRoot && sw.shadowRoot.querySelector('[role="switch"]');
  const track = sw.shadowRoot && sw.shadowRoot.querySelector('.track, .handle, .container');
  const target = track || roleSwitch || sw;
  target.scrollIntoView({ block: 'center', inline: 'center' });
  if (target.focus) target.focus();
  target.click();
  if (roleSwitch) {
    for (const type of ['keydown', 'keyup']) {
      roleSwitch.dispatchEvent(new KeyboardEvent(type, {
        key: ' ',
        code: 'Space',
        bubbles: true,
        composed: true,
      }));
    }
  }
}

function enableSwitch(sw) {
  if (switchState(sw).on) return 'already-on';
  clickSwitch(sw);
  if (switchState(sw).on) return 'toggled-on-click';
  fireSwitchChange(sw, true);
  if (switchState(sw).on) return 'toggled-on-property';
  clickSwitch(sw);
  return switchState(sw).on ? 'toggled-on-retry' : 'failed';
}

function switchFromHost(host) {
  if (!host || !host.querySelector) return null;
  const comp = host.querySelector('arcgis-map-config-time-extent-visibility');
  if (comp) {
    const sr = comp.shadowRoot;
    const sw = (sr && sr.querySelector('calcite-switch')) || comp.querySelector('calcite-switch');
    if (sw) return sw;
  }
  for (const sw of host.querySelectorAll('calcite-switch')) {
    const roleSwitch = sw.shadowRoot && sw.shadowRoot.querySelector('[role="switch"]');
    const label = ((roleSwitch && roleSwitch.getAttribute('aria-label')) || '').toLowerCase();
    if (label.includes('time-based layer visibility')) return sw;
  }
  return null;
}

function findVisibilitySwitch() {
  const seen = new Set();
  const stack = [document.documentElement];
  while (stack.length) {
    const node = stack.pop();
    if (!node || seen.has(node)) continue;
    seen.add(node);

    if (node instanceof Element) {
      if (node.matches('calcite-block.layerproperties-time-block, arcgis-map-config-time-extent-visibility')) {
        const sw = switchFromHost(node);
        if (sw) return sw;
      }
      const sw = switchFromHost(node);
      if (sw) return sw;
      if (node.shadowRoot) stack.push(node.shadowRoot);
      for (let i = node.children.length - 1; i >= 0; i--) stack.push(node.children[i]);
    } else if (node instanceof ShadowRoot) {
      for (let i = node.children.length - 1; i >= 0; i--) stack.push(node.children[i]);
    }
  }
  return null;
}

const sw = findVisibilitySwitch();
return sw ? enableSwitch(sw) : null;
"""


# --------------------------------------------------------------------------- #
# Date helpers
# --------------------------------------------------------------------------- #

def iter_dates(start: dt.date, end: dt.date, step_days: int) -> list[dt.date]:
    dates: list[dt.date] = []
    cur = start
    step = dt.timedelta(days=step_days)
    while cur <= end:
        dates.append(cur)
        cur += step
    return dates


def snapshot_path(date: dt.date) -> Path:
    return OUTPUT_DIR / f"{date.isoformat()}.html"


# --------------------------------------------------------------------------- #
# Browser lifecycle
# --------------------------------------------------------------------------- #

def build_driver(headed: bool) -> webdriver.Chrome:
    mode = "visible" if headed else "headless"
    _log(f"Launching Chrome ({mode}) …")
    opts = Options()
    if not headed:
        opts.add_argument("--headless=new")
        # Software WebGL fallback when no display GPU is available.
        opts.add_argument("--use-angle=swiftshader")
    # ArcGIS Map Viewer requires WebGL2; do NOT pass --disable-gpu.
    opts.add_argument("--window-size=1600,1000")
    opts.add_argument("--enable-webgl")
    opts.add_argument("--ignore-gpu-blocklist")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--log-level=3")
    opts.add_experimental_option("excludeSwitches", ["enable-logging"])
    driver = webdriver.Chrome(options=opts)
    driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT)
    return driver


def _deep_count(driver: webdriver.Chrome, tags: list[str]) -> int:
    try:
        return len(driver.execute_script(_DEEP_QUERY_JS, tags))
    except JavascriptException:
        return 0


def _date_picker_count(driver: webdriver.Chrome) -> int:
    return _deep_count(driver, ["calcite-input-date-picker"])


def _readiness(driver: webdriver.Chrome) -> dict:
    try:
        return driver.execute_script(_READINESS_JS, LAYER_NAME) or {}
    except JavascriptException:
        return {}


def _webgl_error(driver: webdriver.Chrome) -> bool:
    try:
        return "WebGL2 support is required" in driver.page_source
    except JavascriptException:
        return False


def _page_fully_loaded(driver: webdriver.Chrome) -> bool:
    """True when the map shell is interactive enough to open layer Properties.

    Do NOT require the bottom time slider or a large HTML dump here — those appear
    only after the Time panel and visibility toggle are configured.
    """
    if _webgl_error(driver):
        return False
    report = _readiness(driver)
    if not report.get("layerFound", False):
        return False
    if report.get("htmlLen", 0) < MIN_SHELL_HTML_BYTES:
        return False
    return bool(
        report.get("mapView", 0) > 0
        or report.get("shellReady", False)
    ) and bool(report.get("layerPropertiesBtn", False))


def _click_by_label(driver: webdriver.Chrome, label: str, *, exact: bool = True) -> str | None:
    try:
        return driver.execute_script(_CLICK_BY_LABEL_JS, label, exact)
    except JavascriptException:
        return None


def _click_by_text(driver: webdriver.Chrome, needle: str) -> bool:
    try:
        return bool(driver.execute_script(_CLICK_BY_TEXT_JS, needle))
    except JavascriptException:
        return False


def _click_calcite_action_id(driver: webdriver.Chrome, action_id: str) -> bool:
    try:
        return bool(driver.execute_script(_CLICK_CALCITE_ACTION_ID_JS, action_id))
    except JavascriptException:
        return False


def _click_calcite_action_icon(driver: webdriver.Chrome, icon: str) -> str | None:
    try:
        return driver.execute_script(_CLICK_CALCITE_ACTION_ICON_JS, icon)
    except JavascriptException:
        return None


def _probe_properties_panel(driver: webdriver.Chrome) -> dict:
    try:
        return driver.execute_script(_PROBE_PROPERTIES_PANEL_JS) or {}
    except JavascriptException:
        return {}


def _action_state(driver: webdriver.Chrome, action_id: str) -> dict:
    try:
        return driver.execute_script(_ACTION_STATE_JS, action_id) or {}
    except JavascriptException:
        return {}


def _properties_panel_open(driver: webdriver.Chrome) -> bool:
    probe = _probe_properties_panel(driver)
    return bool(
        probe.get("flow")
        or probe.get("timeBlock")
        or probe.get("propertiesExpanded")
    )


def _open_toggle_action(driver: webdriver.Chrome, action_id: str, label: str) -> bool:
    """Click a toolbar toggle only if it is not already expanded (avoids closing panels)."""
    state = _action_state(driver, action_id)
    if state.get("expanded"):
        _log(f"  {label} already open (#{action_id})")
        return True
    if _click_calcite_action_id(driver, action_id):
        _log(f"  opened {label} (#{action_id})")
        return True
    _log(f"  ! could not find {label} (#{action_id})")
    return False


def _select_layer_in_list(driver: webdriver.Chrome) -> bool:
    try:
        return bool(driver.execute_script(_SELECT_LAYER_JS, LAYER_NAME))
    except JavascriptException:
        return False


def _open_properties_panel(driver: webdriver.Chrome) -> bool:
    """Open the Properties side panel once — never double-click the toggle."""
    if _properties_panel_open(driver):
        _log("  Properties panel already open")
        return True

    # Only use the toolbar Properties button (Show properties + Properties was toggling closed).
    if not _open_toggle_action(driver, "layerproperties", "Properties panel"):
        return False

    time.sleep(2.0)
    panel = _wait_for_properties_panel(driver, timeout=30.0)
    _log(
        f"  properties panel: flow={panel.get('flow')}, timeBlock={panel.get('timeBlock')}, "
        f"expanded={panel.get('propertiesExpanded')}"
    )
    return bool(panel.get("flow") or panel.get("timeBlock") or panel.get("propertiesExpanded"))


def _wait_for_properties_panel(driver: webdriver.Chrome, timeout: float = 45.0) -> dict:
    """Wait until the Properties flow or Time block appears in the side panel."""
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        last = _probe_properties_panel(driver)
        if last.get("flow") or last.get("timeBlock"):
            return last
        time.sleep(1.0)
    return last


def _click_layer_options(driver: webdriver.Chrome) -> bool:
    try:
        return bool(driver.execute_script(_CLICK_LAYER_OPTIONS_JS))
    except JavascriptException:
        return False


def _expand_time_block(driver: webdriver.Chrome) -> str | None:
    try:
        return driver.execute_script(_EXPAND_TIME_BLOCK_JS)
    except JavascriptException:
        return None


def _scroll_to_time_section(driver: webdriver.Chrome) -> dict:
    try:
        result = driver.execute_script(_TIME_UI_JS, "scroll") or {}
    except JavascriptException:
        result = {}
    _wheel_scroll_properties_panel(driver, result)
    return result


def _wheel_scroll_properties_panel(driver: webdriver.Chrome, scroll_info: dict) -> None:
    """Wheel-scroll the Properties panel body (calcite-panel .content)."""
    center = scroll_info.get("scrollCenter")
    if not center:
        try:
            center = (driver.execute_script(_TIME_UI_JS, "scroller-info") or {}).get("center")
        except JavascriptException:
            return
    if not center:
        return
    origin = ScrollOrigin.from_viewport(int(center["x"]), int(center["y"]))
    actions = ActionChains(driver)
    actions.scroll_from_origin(origin, 0, 1500).perform()
    actions.scroll_from_origin(origin, 0, 1500).perform()


def _log_scroll_result(scroll: dict) -> None:
    panel = scroll.get("panelScroll") or {}
    _log(
        "  scrolled Properties panel "
        f"(targets={scroll.get('scrollerTargets')}, "
        f"scrollers={panel.get('scrollerCount')}, "
        f"scrolled={panel.get('scrolledCount')}, "
        f"block={scroll.get('blockFound')}, switch={scroll.get('switchFound')})"
    )


def _time_visibility_state(driver: webdriver.Chrome) -> dict:
    try:
        return driver.execute_script(_TIME_UI_JS, "info") or {}
    except JavascriptException:
        return {}


def _click_viewport(driver: webdriver.Chrome, x: float, y: float) -> None:
    """Click at viewport (x, y) using Chrome DevTools + W3C pointer actions."""
    ix, iy = int(x), int(y)
    for event_type in ("mousePressed", "mouseReleased"):
        driver.execute_cdp_cmd(
            "Input.dispatchMouseEvent",
            {
                "type": event_type,
                "x": ix,
                "y": iy,
                "button": "left",
                "clickCount": 1,
            },
        )
    try:
        mouse = PointerInput("mouse", "mouse")
        actions = ActionBuilder(driver, mouse=mouse)
        actions.pointer_action.move_to_location(ix, iy)
        actions.pointer_action.click()
        actions.perform()
    except (AttributeError, JavascriptException):
        # Older Selenium builds lack move_to_location; CDP click above is enough.
        pass


def _enable_time_visibility(driver: webdriver.Chrome) -> str | None:
    _scroll_to_time_section(driver)
    time.sleep(0.5)
    info = _time_visibility_state(driver)
    if not info.get("found"):
        return None
    if info.get("checked"):
        return "already-on"

    rect = info.get("rect") or {}
    x, y = rect.get("x"), rect.get("y")
    w, h = rect.get("width"), rect.get("height")
    if x is None or y is None or not w or not h:
        try:
            return driver.execute_script(_ENABLE_TIME_VISIBILITY_JS)
        except JavascriptException:
            return None

    _click_viewport(driver, x, y)
    time.sleep(0.5)

    info = _time_visibility_state(driver)
    if info.get("checked"):
        return "toggled-on-click"

    _click_viewport(driver, x + max(4, int(w / 4)), y)
    time.sleep(0.5)
    info = _time_visibility_state(driver)
    if info.get("checked"):
        return "toggled-on-click-offset"

    try:
        return driver.execute_script(_ENABLE_TIME_VISIBILITY_JS)
    except JavascriptException:
        return "failed"


def _ensure_time_visibility_on(driver: webdriver.Chrome, attempts: int = 4) -> bool:
    """Scroll the Time section into view and toggle visibility on."""
    for attempt in range(1, attempts + 1):
        scroll = _scroll_to_time_section(driver)
        _log_scroll_result(scroll)
        time.sleep(0.8)

        state = _time_visibility_state(driver)
        if state.get("checked"):
            _log("  time-based layer visibility: already on")
            return True

        result = _enable_time_visibility(driver)
        time.sleep(1.0)
        state = _time_visibility_state(driver)
        rect = state.get("rect") or {}
        _log(
            f"  time-based layer visibility attempt {attempt}: "
            f"{result or 'switch not found'} "
            f"(checked={state.get('checked')}, found={state.get('found')}, "
            f"click=({rect.get('x')}, {rect.get('y')}))"
        )
        if state.get("checked"):
            return True

    return bool(_time_visibility_state(driver).get("checked"))


def _wait_for_date_pickers(driver: webdriver.Chrome, timeout: float = 15.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _date_picker_count(driver) >= 2:
            return True
        time.sleep(0.5)
    return False


def _log(msg: str) -> None:
    print(msg, flush=True)


def wait_for_app_ready(driver: webdriver.Chrome, *, require_date_pickers: bool, headed: bool) -> bool:
    """Wait for the map to finish loading; optionally for the Time date pickers."""
    _log("Loading map (this can take 1–2 minutes on first launch) …")
    deadline = time.time() + PAGE_LOAD_TIMEOUT
    last_progress = 0.0
    last_html = -1
    stalled_since: float | None = None
    while time.time() < deadline:
        try:
            report = _readiness(driver)
            if _webgl_error(driver):
                _log(
                    "  ! Chrome cannot render WebGL2 — ArcGIS will not load. "
                    "Re-run without --headless so a visible Chrome window opens."
                )
                return False

            html_len = report.get("htmlLen", 0)
            if html_len != last_html:
                last_html = html_len
                stalled_since = None
            elif stalled_since is None:
                stalled_since = time.time()
            elif (
                time.time() - stalled_since >= STALL_TIMEOUT
                and not _page_fully_loaded(driver)
            ):
                _log(
                    "  ! Map load stalled "
                    f"(html={html_len:,} bytes, layer={report.get('layerFound', False)}, "
                    f"mapView={report.get('mapView', 0)} for {STALL_TIMEOUT}s)."
                )
                if not headed:
                    _log(
                        "  ! You ran headless — ArcGIS Map Viewer usually needs a visible "
                        "browser. Re-run WITHOUT --headless (visible Chrome is the default)."
                    )
                else:
                    _log(
                        "  ! Check that Chrome opened and the map is not blocked by sign-in "
                        "or a network error."
                    )
                return False

            if _page_fully_loaded(driver):
                if not require_date_pickers or report.get("datePickers", 0) >= 2:
                    _log("Map loaded.")
                    return True
        except NoSuchWindowException:
            _log("  ! Browser window was closed before the map finished loading.")
            return False
        now = time.time()
        if now - last_progress >= 5.0:
            _log(
                "  … still loading "
                f"(html={report.get('htmlLen', 0):,} bytes, "
                f"layer={report.get('layerFound', False)}, "
                f"mapView={report.get('mapView', 0)}, "
                f"shell={report.get('shellReady', False)})"
            )
            last_progress = now
        time.sleep(1.0)

    report = _readiness(driver)
    if _webgl_error(driver):
        _log(
            "  ! Chrome cannot render WebGL2 — ArcGIS will not load. "
            "Re-run without --headless."
        )
        return False
    if not _page_fully_loaded(driver):
        _log(
            "  ! Map did not fully load within "
            f"{PAGE_LOAD_TIMEOUT}s (need layer name + map view or Properties button)."
        )
        if not headed:
            _log("  ! Try again without --headless (visible Chrome is the default).")
    elif require_date_pickers:
        _log("  ! Map loaded but Start/End date pickers are still missing.")
    return False


def open_time_panel(driver: webdriver.Chrome) -> bool:
    """Navigate to layer Time settings, expand the block, and enable visibility."""
    _log("Opening layer Time settings …")

    _open_toggle_action(driver, "layers", "Layers panel")
    time.sleep(1.5)

    if _select_layer_in_list(driver):
        _log(f"  selected layer {LAYER_NAME!r}")
    else:
        _log(f"  ! could not select layer in list; continuing anyway")
    time.sleep(1.0)

    if not _open_properties_panel(driver):
        _log("  ! Properties panel did not stay open; cannot reach Time settings")
        return False

    # Time settings live inside the Properties panel — do not click Properties or
    # Layers again (those buttons are toggles and will close the pane).
    time.sleep(1.0)

    scroll = _scroll_to_time_section(driver)
    _log_scroll_result(scroll)
    time.sleep(1.0)

    expand_result = _expand_time_block(driver)
    _log(f"  expand Time section: {expand_result or 'not found'}")
    time.sleep(1.0)

    scroll = _scroll_to_time_section(driver)
    _log("  scrolled Properties panel again after expand:")
    _log_scroll_result(scroll)
    time.sleep(1.0)

    if not _ensure_time_visibility_on(driver):
        _log("  ! could not enable time-based layer visibility")
        return False
    time.sleep(1.5)

    if _wait_for_date_pickers(driver, timeout=15.0):
        _scroll_to_time_section(driver)
        _log("  time date-pickers are visible")
        return True

    count = _date_picker_count(driver)
    _log(f"  ! only {count} date picker(s) visible after setup")
    return count >= 2


# --------------------------------------------------------------------------- #
# Snapshot logic
# --------------------------------------------------------------------------- #

def set_time_window(driver: webdriver.Chrome, start: dt.date, end: dt.date) -> dict:
    """Set the layer time window to [start, end) and return the JS report."""
    return driver.execute_script(
        _SET_TIME_JS,
        start.isoformat(),
        end.isoformat(),
        "00:00:00",
        "00:00:00",
    )


def save_snapshot(driver: webdriver.Chrome, date: dt.date, screenshots: bool) -> None:
    path = snapshot_path(date)
    path.write_text(driver.page_source, encoding="utf-8")
    if screenshots:
        driver.save_screenshot(str(path.with_suffix(".png")))


def capture_all(
    dates: list[dt.date],
    step_days: int,
    headed: bool,
    screenshots: bool,
    overwrite: bool,
) -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    driver = build_driver(headed)
    failures = 0
    try:
        _log(f"Opening {MAP_VIEWER_URL}")
        driver.get(MAP_VIEWER_URL)
        if not wait_for_app_ready(driver, require_date_pickers=False, headed=headed):
            report = _readiness(driver)
            if not report.get("layerFound"):
                _log("Map did not finish loading; aborting.")
                return len(dates)
            _log(
                "Shell readiness timed out, but the layer is present — "
                "continuing to Time panel setup …"
            )

        if not open_time_panel(driver):
            _log(
                "Could not open the Time panel automatically. "
                "Watch the browser to see whether Properties/Time controls appear."
            )
            return len(dates)

        _log("App ready.")

        for date in dates:
            out = snapshot_path(date)
            if out.exists() and not overwrite:
                _log(f"  = {date} already captured ({out.name}); skipping")
                continue

            window_end = date + dt.timedelta(days=step_days)
            try:
                report = set_time_window(driver, date, window_end)
            except JavascriptException as exc:
                _log(f"  ! {date}: failed to set time window: {exc}")
                report = {"datePickers": 0}

            if report.get("datePickers", 0) < 2:
                _log(
                    f"  ! {date}: only {report.get('datePickers', 0)} date picker(s) "
                    "found; not saving (time window was not set)"
                )
                failures += 1
                continue

            time.sleep(RENDER_SETTLE_SECONDS)
            html_len = len(driver.page_source)
            save_snapshot(driver, date, screenshots)
            _log(
                f"  + {date} -> {out.name} "
                f"(start={report.get('startSet')}, end={report.get('endSet')}, "
                f"{html_len:,} bytes)"
            )
    finally:
        driver.quit()
    return failures


def run_debug(headed: bool, pause_seconds: int) -> int:
    """Load the page and print/dump a full inventory of clickable + time controls."""
    driver = build_driver(headed)
    out_file = OUTPUT_DIR.parent / "selenium" / "debug_controls.json"
    try:
        _log(f"Opening {MAP_VIEWER_URL}")
        if headed:
            _log("A Chrome window should appear. Leave it open until this finishes.")
        driver.get(MAP_VIEWER_URL)
        wait_for_app_ready(driver, require_date_pickers=False, headed=headed)
        open_time_panel(driver)

        inv = driver.execute_script(_INVENTORY_JS)
        inv["readiness"] = _readiness(driver)
        out_file.write_text(json.dumps(inv, indent=2), encoding="utf-8")

        tag_counts = inv.get("tagCounts", {})
        controls = inv.get("controls", [])
        timeish = inv.get("timeish", [])

        _log(f"\nReadiness: {inv.get('readiness')}")
        _log(f"\nCustom element tags ({len(tag_counts)}):")
        for tag, n in sorted(tag_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:25]:
            _log(f"  {n:4d}  {tag}")

        _log(f"\nClickable controls ({len(controls)}):")
        for c in controls[:40]:
            _log(f"  [{c['tag']}] {c['name']!r}")

        _log(f"\nTime/date-related elements ({len(timeish)}):")
        for t in timeish[:20]:
            _log(f"  [{t['tag']}] name={t['name']!r} value={t['value']!r}")

        _log(f"\nFull inventory written to {out_file}")
        if pause_seconds > 0:
            _log(f"Leaving the browser open for {pause_seconds}s so you can inspect the UI …")
            time.sleep(pause_seconds)
    finally:
        driver.quit()
    return 0


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Save Map Viewer HTML snapshots for scrape_trees.py."
    )
    p.add_argument("--start", type=dt.date.fromisoformat, default=DEFAULT_START)
    p.add_argument("--end", type=dt.date.fromisoformat, default=DEFAULT_END)
    p.add_argument("--step", type=int, default=DEFAULT_STEP_DAYS,
                   help="Days between snapshots / time-window length (default 7).")
    p.add_argument("--headless", action="store_true",
                   help="Headless Chrome (often breaks WebGL on ArcGIS).")
    p.add_argument("--headed", action="store_true", help="No-op; visible is default.")
    p.add_argument("--screenshots", action="store_true")
    p.add_argument("--overwrite", action="store_true")
    p.add_argument("--debug", action="store_true")
    p.add_argument("--debug-pause", type=int, default=20)
    return p.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    headed = not args.headless
    if args.debug:
        return run_debug(headed, args.debug_pause)

    if args.step < 1:
        _log("step must be >= 1")
        return 2
    dates = iter_dates(args.start, args.end, args.step)
    _log(
        f"Capturing {len(dates)} snapshot(s) "
        f"({args.start} .. {args.end}, step={args.step}d)"
    )
    if args.headless:
        _log("headless mode — if nothing loads, drop --headless")
    failures = capture_all(
        dates, args.step, headed, args.screenshots, args.overwrite
    )
    if failures:
        _log(
            f"Done with {failures} snapshot(s) where the time window could not be "
            "confirmed. Inspect those files or run --debug."
        )
        return 1
    _log("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
