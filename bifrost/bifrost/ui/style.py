"""
GTK CSS for the stock widgets (buttons, entries, lists, scales, dropdowns).

Valhalla paints almost everything by hand because a monitor only displays. This
app also has to take input - text search, file lists, sliders - and re-implementing
a text entry in Cairo would be a bad trade, so the interactive widgets are real
GTK widgets restyled in the same palette. Colours here are the theme.py palette
written out as hex; keep the two in step.
"""

CSS = """
window.bifrost, window.bifrost > * { background-color: #04060c; color: #c9d8ef; }
headerbar.bifrost {
    background-color: #070c16; border-bottom: 1px solid #1b2a45;
    min-height: 40px; box-shadow: none;
}
headerbar.bifrost windowcontrols button { color: #7288a8; }

.bifrost label { font-family: "Ubuntu Sans"; }
.bifrost .mono { font-family: "Ubuntu Mono"; }
.bifrost .dim { color: #7288a8; }
.bifrost .mute { color: #42536e; }
.bifrost .title-big { font-size: 20px; font-weight: 700; color: #f2c877; }
.bifrost .artist { font-size: 13px; color: #c9d8ef; }
.bifrost .readout { font-family: "Ubuntu Mono"; font-size: 30px; color: #4fd6ff; }
.bifrost .badge {
    font-size: 10px; font-weight: 700; letter-spacing: 1px; color: #31d3a4;
    border: 1px solid #1f6f5a; border-radius: 3px; padding: 0 6px;
}
.bifrost .badge.nd { color: #ffab2e; border-color: #7a5316; }
.bifrost .status { font-size: 11px; color: #7288a8; }
.bifrost .error { color: #ff7a6e; }

.bifrost button {
    background: #0a1120; background-image: none; color: #c9d8ef;
    border: 1px solid #1b2a45; border-radius: 4px; box-shadow: none;
    padding: 5px 14px; min-height: 22px; text-shadow: none;
}
.bifrost button:hover { background: #111b30; border-color: #26374f; color: #f2c877; }
.bifrost button:active, .bifrost button:checked {
    background: #14233f; border-color: #c8963c; color: #f2c877;
}
.bifrost button:disabled { color: #42536e; border-color: #141e33; }
.bifrost button.primary { border-color: #9a7434; color: #f2c877; }
.bifrost button.primary:hover { background: #1a1a22; border-color: #c8963c; }
.bifrost button.tab { border-radius: 0; border: none; border-bottom: 2px solid transparent;
    background: transparent; letter-spacing: 2px; font-weight: 700; padding: 8px 18px; }
.bifrost button.tab:checked { border-bottom-color: #c8963c; color: #f2c877; background: transparent; }
.bifrost button.tab:hover { background: #0a1120; }
.bifrost button.preset { padding: 6px 16px; }
.bifrost button.preset:checked { border-color: #4fd6ff; color: #4fd6ff; }

.bifrost entry, .bifrost searchentry {
    background: #070c16; color: #c9d8ef; border: 1px solid #1b2a45;
    border-radius: 4px; box-shadow: none; min-height: 26px;
}
/* the inner text node must not draw a second frame inside the entry's own */
.bifrost entry text, .bifrost searchentry text {
    background: transparent; border: none; box-shadow: none; outline: none; min-height: 0;
}
.bifrost entry:focus-within, .bifrost searchentry:focus-within { border-color: #4fd6ff; }

.bifrost dropdown > button { padding: 4px 10px; }
.bifrost popover contents, .bifrost popover > contents {
    background: #0a1120; border: 1px solid #26374f; color: #c9d8ef;
}
.bifrost popover listview, .bifrost popover row { background: transparent; color: #c9d8ef; }
.bifrost popover row:hover, .bifrost popover row:selected { background: #14233f; }

.bifrost list, .bifrost listview { background: transparent; }
.bifrost row { background: transparent; border-bottom: 1px solid #101a2d; padding: 2px 4px; }
.bifrost row:hover { background: #0d1628; }
.bifrost row:selected { background: #14233f; }

.bifrost scale trough { background: #141e33; min-height: 4px; border-radius: 2px; border: none; }
.bifrost scale highlight { background: #4fd6ff; border-radius: 2px; }
.bifrost scale slider {
    background: #f2c877; min-width: 14px; min-height: 14px; margin: 0; border-radius: 7px;
    border: none; box-shadow: 0 0 6px #c8963c;
}
.bifrost progressbar trough { background: #141e33; min-height: 4px; border-radius: 2px; border: none; }
.bifrost progressbar progress { background: #31d3a4; border-radius: 2px; min-height: 4px; }

.bifrost switch { background: #141e33; border: 1px solid #1b2a45; border-radius: 12px; }
.bifrost switch:checked { background: #1b4a6b; border-color: #4fd6ff; }
.bifrost switch slider { background: #c9d8ef; border-radius: 10px; }

.bifrost scrolledwindow, .bifrost viewport { background: transparent; }
.bifrost scrollbar slider { background: #26374f; border-radius: 3px; min-width: 6px; }
.bifrost scrollbar slider:hover { background: #42536e; }
.bifrost tooltip, .bifrost tooltip.background { background: #0a1120; color: #c9d8ef;
    border: 1px solid #26374f; }
"""
