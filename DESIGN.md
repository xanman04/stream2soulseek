# S2S native interface

S2S is an operational desktop music tool. Preserve native PySide6 controls, the supplied S2S logo,
service identities and the existing blue accent. Prioritize clear actions, readable track data and
predictable review over decorative effects. Download approval and transfer behavior belong to the
workflow and must not change as a side effect of visual work.

- Use the light/dark palettes in `ui/theme.py`; do not introduce independent page palettes.
- Use system UI typography, a 26px page heading, a 17px section heading and compact body copy.
- Navigation uses consistent line icons and bordered buttons, with a filled active destination.
  Keep the sidebar 264 logical pixels wide and its density stable as windows grow. Its sections
  scroll on smaller windows; connection and Settings controls stay accessible at the bottom.
- Home uses square provider artwork, up to two title lines and secondary service/track information.
  Missing images use an honest service-mark fallback. Never invent artwork or listening history.
  Cache provider images in per-user app data, independently of audio files and release assets.
- Home actions occupy their natural content height; additional window height stays outside them.
- Group search mode and query fields in one workspace, with results and local filters immediately
  below. Free search retains raw results; Quick search displays its matching rules.
- Joined mode buttons have no layout gap. Active filter chips wrap and can each be removed.
- Use `ui/icons.py` for palette-aware native line icons. Primary and selected controls need
  contrasting icons in both themes. Preserve keyboard focus and explicit accessible action names.
- Verify wide/compact native Windows renders, both themes, empty/loading/error states, long titles,
  saved transfers and optional-image failures. No Qt hover tooltips or app-wide event filters.
