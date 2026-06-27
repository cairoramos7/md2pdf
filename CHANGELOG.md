# Changelog

All notable changes to this project will be documented in this file.

## [1.1.0] - 2026-06-27

### Added
- **Mermaid Layout Control**: Added a dropdown select in the header to switch between **Adaptive** and **Hierarchical** Mermaid layouts.
- **ELK Layout Engine Integration**: Imported and registered `@mermaid-js/layout-elk@0.2.2` layout loaders to support advanced and clean orthogonal flowchart layouts.

### Changed
- **Mermaid Layout Mappings**:
  - **Hierarchical**: Uses the `dagre` engine (curved, fluid lines) with natural scaling and horizontal scrolling.
  - **Adaptive**: Uses the `elk` engine (orthogonal, clean squared lines) auto-scaling to fit the page width.

### Fixed
- **Dark Mode PDF Export Bug**: Forced Playwright media emulation to use `color_scheme='light'`. This prevents headless Chromium from inheriting the system's Dark Mode, which previously caused Mermaid nodes to render as solid black blocks in the output PDF.
