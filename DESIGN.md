# S2S native interface

S2S is an operational desktop music tool. Preserve native PySide6 controls, the supplied S2S logo,
service identities and the existing blue accent. Prioritize clear actions, readable track data and
predictable review over decorative effects. Download approval and transfer behavior belong to the
workflow and must not change as a side effect of visual work.

- Use the light/dark palettes in `ui/theme.py`; do not introduce independent page palettes.
- Use system UI typography, a 30px page heading, a 17px section heading and compact body copy. Headings use
  the bundled Clash Display through `theme.display_label`; body text stays in the system font.
- Settings is a dialog with a navigation list on the left and one scrolling page per section
  (`ui/settings_layout.py`): a 22px page title, titled sections on one quiet panel, each field with its label above
  and muted help text below. Panels never nest; tick boxes and radio buttons use the shared themed indicators.
- Dialogs for different things stay different: the regular update window is for stable releases only, and sponsor
  preview builds use their own window. Menus name actions plainly ("About", "Quit"); only "Support S2S" carries the
  app name.
- Buttons that open a dialog or menu with the mouse do not keep a focus ring afterwards; keyboard focus stays visible.
- Navigation uses consistent line icons and bordered buttons, with a filled active destination.
  Keep the sidebar 264 logical pixels wide and never let it scroll. It steps down a ladder of tighter layouts
  until everything fits: slightly smaller rows, then one saved transfer on a single line with a count. Streaming
  services, the Soulseek connection and Settings are always fully visible at the bottom. The sidebar never lists
  the six playlist steps: Playlist transfer is one button that opens Choose playlist (or the step the run was left
  on), and it is filled while any workflow page shows.
- The six steps are a step bar in the header row of every workflow page (`ui/steps.py`), right of the title: numbered
  circles joined by lines, a check once a step is done, the current step filled, steps that are not open yet dimmed
  and not clickable. It shares the title's row, so it costs a page no height (Downloads and Check files keep every
  pixel); it shows one-word step names under the circles when the row is wide enough and drops them before it would
  crowd the title. Nodes are real buttons: Tab reaches them, a mouse click leaves no focus ring, and each has an
  accessible name with its state.
- Every page opens with the same header (`ui/page_header.py`): the page title at 30px in the display face, an
  optional muted line saying what the page is for (Home, Find a track, My Playlists, Transfer history; not on the
  workflow pages, where the step bar gives the context and the height is precious), optional controls on the right,
  and a hairline rule. Pages share one left edge and one top margin so titles sit in the same place everywhere.
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
