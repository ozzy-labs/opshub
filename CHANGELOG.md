# Changelog

All notable changes to OpsHub will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.3.1](https://github.com/ozzy-labs/opshub/compare/v0.3.0...v0.3.1) (2026-06-03)


### Added

* **config:** load opshub.toml at runtime via TomlConfigSettingsSource ([#418](https://github.com/ozzy-labs/opshub/issues/418)) ([#423](https://github.com/ozzy-labs/opshub/issues/423)) ([144b640](https://github.com/ozzy-labs/opshub/commit/144b640fe9aa5c327fc09d2b2c5dcd9c2085e569))


### Documentation

* **adr:** ADR-0032 runtime TOML config loading + cross-refs ([#421](https://github.com/ozzy-labs/opshub/issues/421)) ([c99416b](https://github.com/ozzy-labs/opshub/commit/c99416b15fd156a42b7b374f2a20c5adfb425be5)), closes [#417](https://github.com/ozzy-labs/opshub/issues/417)
* **adr:** fix ADR-0032 reference to --print-paths flag ([#424](https://github.com/ozzy-labs/opshub/issues/424)) ([1c01a63](https://github.com/ozzy-labs/opshub/commit/1c01a63f57f11c2eacbd5b7dce90749e63ec18b7))
* align operator-facing docs with ADR-0032 TOML loading ([#420](https://github.com/ozzy-labs/opshub/issues/420)) ([bec0bc9](https://github.com/ozzy-labs/opshub/commit/bec0bc9e36f0e6614782a3674f2ee7f74873160c))
* **config:** post-[#416](https://github.com/ozzy-labs/opshub/issues/416) audit followup — cross-ref ADR-0032 from setup docs and starter TOML ([#425](https://github.com/ozzy-labs/opshub/issues/425)) ([e046478](https://github.com/ozzy-labs/opshub/commit/e04647893fd818cdf2b01a36a70f7475d35556c0))

## [0.3.0](https://github.com/ozzy-labs/opshub/compare/v0.2.11...v0.3.0) (2026-06-03)


### ⚠ BREAKING CHANGES

* reorganise CLI command surface to noun-first per-noun group (Phase 17-B) ([#414](https://github.com/ozzy-labs/opshub/issues/414))

### Added

* reorganise CLI command surface to noun-first per-noun group (Phase 17-B) ([#414](https://github.com/ozzy-labs/opshub/issues/414)) ([6be57e9](https://github.com/ozzy-labs/opshub/commit/6be57e93b90f56c465ee1ffa3370bb888dbc311e))


### Documentation

* **adr:** ADR-0031 — CLI command surface organization (Phase 17-A) ([#412](https://github.com/ozzy-labs/opshub/issues/412)) ([e2cf1c2](https://github.com/ozzy-labs/opshub/commit/e2cf1c22954827303a8d9cbaa6e87ab5b40dce44))
* **upgrading:** rewrite legacy opshub connector ... examples to Phase 17 form ([#415](https://github.com/ozzy-labs/opshub/issues/415)) ([e6530c2](https://github.com/ozzy-labs/opshub/commit/e6530c2bededb56659e10718503cdb64c0be21fa))

## [0.2.11](https://github.com/ozzy-labs/opshub/compare/v0.2.10...v0.2.11) (2026-06-02)


### Added

* **slack:** emit per-row debug log on inaccessible-channel skip ([#407](https://github.com/ozzy-labs/opshub/issues/407)) ([7c6cb3a](https://github.com/ozzy-labs/opshub/commit/7c6cb3ac4761ea287ea1079113eb5da20fcafb97))


### Fixed

* **slack:** drop context-mismatched ADR-0018 §(7) reference from inaccessible-channels warning ([#406](https://github.com/ozzy-labs/opshub/issues/406)) ([60484ea](https://github.com/ozzy-labs/opshub/commit/60484ea9e71bf7f460653b04a6728ef27feaf63e))
* **slack:** skip inaccessible channels in conversations --since probe ([#405](https://github.com/ozzy-labs/opshub/issues/405)) ([2e53815](https://github.com/ozzy-labs/opshub/commit/2e5381577b9c2b153e1f6c84b340bbf264c88eb3))


### Changed

* rename "secretary" terminology to "assistant" across the repo ([#404](https://github.com/ozzy-labs/opshub/issues/404)) ([9edf1be](https://github.com/ozzy-labs/opshub/commit/9edf1be642d4c15940689ac7ec5b0af6dbf6b67d))


### Documentation

* **adr:** add ADR-0030 Slack thread reply ingestion policy ([#402](https://github.com/ozzy-labs/opshub/issues/402)) ([9247d37](https://github.com/ozzy-labs/opshub/commit/9247d3754c0f9eeaaecd4321ec7018776c6fba13))
* **slack:** point operators at --debug for per-channel skip ids ([#408](https://github.com/ozzy-labs/opshub/issues/408)) ([e0f2de8](https://github.com/ozzy-labs/opshub/commit/e0f2de8dbe651c0f36d5c72f5f7928fcca0c1d7d))

## [0.2.10](https://github.com/ozzy-labs/opshub/compare/v0.2.9...v0.2.10) (2026-06-02)


### Fixed

* **skills:** make 'opshub skills install' read from docs/skills/ in editable mode ([#399](https://github.com/ozzy-labs/opshub/issues/399)) ([9c5c490](https://github.com/ozzy-labs/opshub/commit/9c5c49044e60ede7442c5a02a5b2c7c660a11a0f))

## [0.2.9](https://github.com/ozzy-labs/opshub/compare/v0.2.8...v0.2.9) (2026-06-02)


### Added

* **cli:** add opshub skills install / list (Phase 16-B) ([#390](https://github.com/ozzy-labs/opshub/issues/390)) ([836e8cf](https://github.com/ozzy-labs/opshub/commit/836e8cf8ea3f93bca225d791785e0a148bcb1f7d))
* **cli:** wire opshub init to opshub skills install (Phase 16-C) ([#392](https://github.com/ozzy-labs/opshub/issues/392)) ([a88ef48](https://github.com/ozzy-labs/opshub/commit/a88ef488de0b409430c4c73f1fd43f5f51496ee2))
* **connectors/slack,cli:** add type sort + --since filter to conversations command ([#374](https://github.com/ozzy-labs/opshub/issues/374)) ([#375](https://github.com/ozzy-labs/opshub/issues/375)) ([3ff8d08](https://github.com/ozzy-labs/opshub/commit/3ff8d08c8b55b3a22135688d2ed5c083dfc22d3e))


### Changed

* **connectors/slack:** extract retry_on_rate_limit helper to dedupe history call paths ([#377](https://github.com/ozzy-labs/opshub/issues/377)) ([#378](https://github.com/ozzy-labs/opshub/issues/378)) ([4fabc81](https://github.com/ozzy-labs/opshub/commit/4fabc81117bb42958d9046f66376f1e86a339e4c))
* **connectors/slack:** migrate _call_list to retry_on_rate_limit helper ([#379](https://github.com/ozzy-labs/opshub/issues/379)) ([#380](https://github.com/ozzy-labs/opshub/issues/380)) ([1963360](https://github.com/ozzy-labs/opshub/commit/1963360946c11a29e02b3682b1c4fe3871b0b3cb))


### Documentation

* **adr,skills:** add ADR-0029 distribute secretary skills via opshub package bundling (Phase 16-A) ([#389](https://github.com/ozzy-labs/opshub/issues/389)) ([af8b6c4](https://github.com/ozzy-labs/opshub/commit/af8b6c4cf517e8f8ed3ca052caf0ec0272d76d1b))
* **agents:** apply PR [#387](https://github.com/ozzy-labs/opshub/issues/387) review info findings (Retry-After wording + Phase 16 criterion) ([#388](https://github.com/ozzy-labs/opshub/issues/388)) ([6bc2cd9](https://github.com/ozzy-labs/opshub/commit/6bc2cd95089243bea2f0465d37f4b02f6b46e1ed))
* **agents:** extract Post-Phase 15 Maintenance from Status run-on into bulleted section ([#387](https://github.com/ozzy-labs/opshub/issues/387)) ([b2f35fb](https://github.com/ozzy-labs/opshub/commit/b2f35fb2a6dcd25220ac060648c72e2b7589d51f))
* fix Phase 16 audit gaps (skills-sync-check description + stale Phase 15+ defer mentions) ([#394](https://github.com/ozzy-labs/opshub/issues/394)) ([44bca39](https://github.com/ozzy-labs/opshub/commit/44bca39ab16c2c461de4ce5f666526232fc76876))

## [0.2.8](https://github.com/ozzy-labs/opshub/compare/v0.2.7...v0.2.8) (2026-06-02)


### Added

* **connectors/slack,cli:** replace channels command with conversations (users.conversations + DM/MPIM + progress) ([#369](https://github.com/ozzy-labs/opshub/issues/369)) ([48b8eb9](https://github.com/ozzy-labs/opshub/commit/48b8eb9e00147ad4bcfea5946728aa995597f999))
* **connectors/slack:** include body excerpt in search title + bot/system message fallback ([#368](https://github.com/ozzy-labs/opshub/issues/368)) ([e511db5](https://github.com/ozzy-labs/opshub/commit/e511db5c9cf667f10a7c6448578de52b247cf697))


### Fixed

* **connectors/slack:** satisfy mypy strict redundant-cast + unused-ignore in conversations ([#373](https://github.com/ozzy-labs/opshub/issues/373)) ([a8287a0](https://github.com/ozzy-labs/opshub/commit/a8287a0aa9bef4492d6674392d50b93ee2bb660f))


### Documentation

* **slack:** align README/architecture/troubleshooting/mcp-setup/CLAUDE/AGENTS with conversations command + DM/MPIM scope ([#371](https://github.com/ozzy-labs/opshub/issues/371)) ([9105a4b](https://github.com/ozzy-labs/opshub/commit/9105a4bed9cbaeb5393841c94f41f8f4bd0e12ca))

## [0.2.7](https://github.com/ozzy-labs/opshub/compare/v0.2.6...v0.2.7) (2026-06-02)


### Added

* **cli,mcp:** connector sync / MCP の --debug opt-in 経路 (デフォルト型名のみ維持、T3 of [#317](https://github.com/ozzy-labs/opshub/issues/317)) ([#330](https://github.com/ozzy-labs/opshub/issues/330)) ([f3ef9cd](https://github.com/ozzy-labs/opshub/commit/f3ef9cd76c268d465a0c5b1ec21b130ad2dc039b))
* **cli:** root callback で --verbose / --quiet / --debug / --log-format / --log-file を提供 (T2 of [#317](https://github.com/ozzy-labs/opshub/issues/317)) ([#331](https://github.com/ozzy-labs/opshub/issues/331)) ([2191dba](https://github.com/ozzy-labs/opshub/commit/2191dbaa8883f4906045b7f493bd3d6780ca3dc3))
* **cli:** show determinate progress for embeddings and projections rebuild ([#325](https://github.com/ozzy-labs/opshub/issues/325)) ([f3b21a6](https://github.com/ozzy-labs/opshub/commit/f3b21a6b032238b8e92e41236c0dc97434878deb))
* **cli:** show progress indicator during connector sync ([#323](https://github.com/ozzy-labs/opshub/issues/323)) ([ec48b06](https://github.com/ozzy-labs/opshub/commit/ec48b069b9df225bb1f981c6b0ccab84facf135e))
* **connectors/slack,cli:** add opshub connector slack channels listing command ([#361](https://github.com/ozzy-labs/opshub/issues/361)) ([780206a](https://github.com/ozzy-labs/opshub/commit/780206ad949a36b11ba3c0a647fe7e0308b508b9))
* **connectors/slack:** add conversations.list discovery helper ([#344](https://github.com/ozzy-labs/opshub/issues/344)) ([44fcfb0](https://github.com/ozzy-labs/opshub/commit/44fcfb0ec5906e32f7d66acee6c31ea633724b0d))
* **core:** redaction processor + log settings resolver + debug traceback helper (ADR-0027, T1 of [#317](https://github.com/ozzy-labs/opshub/issues/317)) ([#329](https://github.com/ozzy-labs/opshub/issues/329)) ([9323b7a](https://github.com/ozzy-labs/opshub/commit/9323b7af5705ede59acfc64385cad662e7979dbb))
* **db:** migration 0028 — fts5 tokenizer trigram (Phase 15 S2) ([#363](https://github.com/ozzy-labs/opshub/issues/363)) ([1a62fa5](https://github.com/ozzy-labs/opshub/commit/1a62fa5d363314b2e3008ce54ee3daa36ad031b6))
* **services/search:** short-query LIKE fallback + japanese e2e (Phase 15 S3) ([#364](https://github.com/ozzy-labs/opshub/issues/364)) ([8350161](https://github.com/ozzy-labs/opshub/commit/8350161b0ba0fd860170ed935cb72fc0aec2922f))


### Fixed

* **connectors/slack,services:** treat whitespace-only summary as missing ([#340](https://github.com/ozzy-labs/opshub/issues/340)) ([82e3a25](https://github.com/ozzy-labs/opshub/commit/82e3a25827d5552e64378f70a0f15c77e6396482))
* **connectors/slack:** advance cursor monotonically across paginated history (PR 1 of [#339](https://github.com/ozzy-labs/opshub/issues/339)) ([#345](https://github.com/ozzy-labs/opshub/issues/345)) ([756410b](https://github.com/ozzy-labs/opshub/commit/756410b7f23c939e30e98b733797ccd4ec2ad962))
* **connectors/slack:** handle empty-text messages in inbox enqueue ([#336](https://github.com/ozzy-labs/opshub/issues/336)) ([329d3ba](https://github.com/ozzy-labs/opshub/commit/329d3ba3d69801e91cc19678724db6be74d55d4f))
* **connectors/slack:** track max ts per channel + advance cursor on partial sync ([#362](https://github.com/ozzy-labs/opshub/issues/362)) ([2e2b1f9](https://github.com/ozzy-labs/opshub/commit/2e2b1f9e4e104eaabecde5bbd482a06ef9213996)), closes [#339](https://github.com/ozzy-labs/opshub/issues/339)
* **connectors/teams:** normalise empty summary to None for symmetry ([#335](https://github.com/ozzy-labs/opshub/issues/335)) ([1b7a5bd](https://github.com/ozzy-labs/opshub/commit/1b7a5bd147a7e4154e1ad396c029f0108b2c2a6a))
* **connectors/teams:** treat whitespace-only summary as missing ([#342](https://github.com/ozzy-labs/opshub/issues/342)) ([e6c09a0](https://github.com/ozzy-labs/opshub/commit/e6c09a0513765a66a8156eeae674c6a40b518dca))
* **connectors:** apply normalise_optional_text to url field for whitespace SSOT symmetry ([#357](https://github.com/ozzy-labs/opshub/issues/357)) ([b1f8c44](https://github.com/ozzy-labs/opshub/commit/b1f8c44cb683a7d0914eedbcfb6f3593164017da))
* **connectors:** SSOT whitespace-only summary normalisation across helper-based mappers ([#355](https://github.com/ozzy-labs/opshub/issues/355)) ([8395525](https://github.com/ozzy-labs/opshub/commit/8395525165a7e7823b1dac1ebd54a2cf5d1f0ab8))
* **core,cli,docs:** -vv で OPSHUB_DEBUG export + connector sync stderr 訂正 + フラグ衝突 test (epic [#317](https://github.com/ozzy-labs/opshub/issues/317) audit followup) ([#334](https://github.com/ozzy-labs/opshub/issues/334)) ([4431789](https://github.com/ozzy-labs/opshub/commit/443178962c2bb7ececa4fb4af318aef692eb916e))
* **tests/cli:** isolate test_connector_auth.py from sibling monkeypatch pollution ([#348](https://github.com/ozzy-labs/opshub/issues/348)) ([#351](https://github.com/ozzy-labs/opshub/issues/351)) ([b00c158](https://github.com/ozzy-labs/opshub/commit/b00c158be15edf8a801ef2e68a4cb71eadddb78f))


### Documentation

* ADR-0028 (FTS5 Japanese tokenizer trigram) + phase-15-plan + index 追加 ([#346](https://github.com/ozzy-labs/opshub/issues/346)) ([bf88898](https://github.com/ozzy-labs/opshub/commit/bf8889812e12d789ef3b41e2afa8e230b4cf32dd))
* **adr,core,troubleshooting:** close audit gaps from [#332](https://github.com/ozzy-labs/opshub/issues/332)/[#337](https://github.com/ozzy-labs/opshub/issues/337)/[#343](https://github.com/ozzy-labs/opshub/issues/343) (whitespace normalisation contract) ([#356](https://github.com/ozzy-labs/opshub/issues/356)) ([5ea9469](https://github.com/ozzy-labs/opshub/commit/5ea94699fafb89d2c11ec90be351c72d303c8e20))
* **architecture,adr:** close epic [#316](https://github.com/ozzy-labs/opshub/issues/316) post-merge audit doc gaps (§2.1 / structlog decision / OPSHUB_PROGRESS values / AGENTS / CLAUDE) ([#327](https://github.com/ozzy-labs/opshub/issues/327)) ([379d0e8](https://github.com/ozzy-labs/opshub/commit/379d0e88b1b790d2cab89bfecbd4a088eefd2598))
* document CLI progress reporting (ADR-0026) ([#326](https://github.com/ozzy-labs/opshub/issues/326)) ([92d8d72](https://github.com/ozzy-labs/opshub/commit/92d8d724c04c8354b5df248305800025e3dd6d04))
* fix pre-existing MD033 inline HTML in architecture.md ([#353](https://github.com/ozzy-labs/opshub/issues/353)) ([1b0c162](https://github.com/ozzy-labs/opshub/commit/1b0c16262bf126c99579e2fb5794dea15a7f7638))
* **phase-15:** closeout — troubleshooting / AGENTS / search --help (Phase 15 S4) ([#365](https://github.com/ozzy-labs/opshub/issues/365)) ([843cc0a](https://github.com/ozzy-labs/opshub/commit/843cc0a48f513e786035708709a54096a6b1cef6))
* post-merge audit followup for [#344](https://github.com/ozzy-labs/opshub/issues/344)/[#345](https://github.com/ozzy-labs/opshub/issues/345)/[#346](https://github.com/ozzy-labs/opshub/issues/346) (architecture / channels docstring / phase-15-plan / decisions-log) ([#350](https://github.com/ozzy-labs/opshub/issues/350)) ([cb38429](https://github.com/ozzy-labs/opshub/commit/cb3842956c99520dd70c72b087c2e4e4ccd50f41))
* troubleshooting docs + SECURITY closeout (ADR-0027, T4 of [#317](https://github.com/ozzy-labs/opshub/issues/317)) ([#333](https://github.com/ozzy-labs/opshub/issues/333)) ([465e949](https://github.com/ozzy-labs/opshub/commit/465e9490732e7d8a15166d77d448bc0b132b4e39))

## [0.2.6](https://github.com/ozzy-labs/opshub/compare/v0.2.5...v0.2.6) (2026-05-31)


### Added

* **connectors/google_calendar:** events api sync token + master event mapper + override + cursor + fallback (Phase 14 G4) ([#301](https://github.com/ozzy-labs/opshub/issues/301)) ([a4cefab](https://github.com/ozzy-labs/opshub/commit/a4cefab227439a4316c07bd6898766980c127835))
* **connectors/google_mail:** gmail api history + message mapper + cursor + fallback ([#303](https://github.com/ozzy-labs/opshub/issues/303)) ([3c621c1](https://github.com/ozzy-labs/opshub/commit/3c621c1d27827f60162e469d3674cf0b7dd49db7))


### Changed

* **connectors/google_auth:** shared OAuth foundation + 3-scope expansion (Phase 14 G2) ([#300](https://github.com/ozzy-labs/opshub/issues/300)) ([bd74191](https://github.com/ozzy-labs/opshub/commit/bd741916ac880fa12b5e2b44828e797b27c0ec58))


### Documentation

* **adr,plan:** adr-0010 + 0014 amendments + phase-14-plan (Gmail + Google Calendar) ([#298](https://github.com/ozzy-labs/opshub/issues/298)) ([f5d43c8](https://github.com/ozzy-labs/opshub/commit/f5d43c8e6819e07cfcf951bab735b6b870fe31e1))
* phase 14 audit cluster B2 (Phase 14+ → 15+ unification + anchor links + outlook + mapper symmetry counts) ([#312](https://github.com/ozzy-labs/opshub/issues/312)) ([02667d7](https://github.com/ozzy-labs/opshub/commit/02667d701c644bc7eda1f324f50081efa85db6d0)), closes [#307](https://github.com/ozzy-labs/opshub/issues/307)
* phase 14 audit cluster C (operator-facing docs vs code drift) ([#310](https://github.com/ozzy-labs/opshub/issues/310)) ([bfefa82](https://github.com/ozzy-labs/opshub/commit/bfefa82f21964af590852c0fe40b94acd8723d01))
* phase 14 closeout (docs + e2e + status lines + phase-13 forecast realign) ([#304](https://github.com/ozzy-labs/opshub/issues/304)) ([ab0b792](https://github.com/ozzy-labs/opshub/commit/ab0b79224c849050a26d18cc0e1817de9a970c82))
* **skills:** phase 14 audit cluster B1 (SKILL.md vocabulary 反映漏れ修正) ([#311](https://github.com/ozzy-labs/opshub/issues/311)) ([16504fc](https://github.com/ozzy-labs/opshub/commit/16504fcc9708b29e9b5465cbb986ec13be540805))

## [0.2.5](https://github.com/ozzy-labs/opshub/compare/v0.2.4...v0.2.5) (2026-05-31)


### Added

* **connectors/google_workspace:** oauth (ms365 pattern) + drive api metadata + rate-limit retry ([#283](https://github.com/ozzy-labs/opshub/issues/283)) ([45d0acf](https://github.com/ozzy-labs/opshub/commit/45d0acfcf33c6cd56adae83415cfabc35be0c084))
* **connectors/google_workspace:** workspace export body + provenance + content_extraction wiring ([#284](https://github.com/ozzy-labs/opshub/issues/284)) ([ec63da8](https://github.com/ozzy-labs/opshub/commit/ec63da89fe1ee966a950431b55ebaea8284edd09))
* **core,domain:** workspace export path + google workspace source_type literals ([#282](https://github.com/ozzy-labs/opshub/issues/282)) ([23dce1c](https://github.com/ozzy-labs/opshub/commit/23dce1c9b14e3b2bd62c3661bd47f1bcdbd37a09))


### Fixed

* **connectors/google_workspace:** TTL fallback full-pass + WARNING log (ADR-0010 §改訂 (g) 整合) ([#290](https://github.com/ozzy-labs/opshub/issues/290)) ([26b150e](https://github.com/ozzy-labs/opshub/commit/26b150ef317476d225051f8590615611c9752b6c))


### Documentation

* **adr,plan:** adr-0010 + 0014 + 0025 revisions + phase-13-plan (Google Workspace) ([#280](https://github.com/ozzy-labs/opshub/issues/280)) ([ea32e5d](https://github.com/ozzy-labs/opshub/commit/ea32e5d5e2354c38a6b86cbeb6716fe161560c6f))
* phase 13 audit cluster B (docs drift + phase 11 residual + phase 13+ → 14+ unification) ([#291](https://github.com/ozzy-labs/opshub/issues/291)) ([2192829](https://github.com/ozzy-labs/opshub/commit/219282933124e25ca4f016a4ba31aba561f48924))
* phase 13 closeout (docs + e2e + guard + phase-12 forecast realign) ([#285](https://github.com/ozzy-labs/opshub/issues/285)) ([026f67c](https://github.com/ozzy-labs/opshub/commit/026f67c43dd845726ff20e2782fb5cda325496fa))

## [0.2.4](https://github.com/ozzy-labs/opshub/compare/v0.2.3...v0.2.4) (2026-05-31)


### Added

* **config,mcp:** wire office settings + register teams/onedrive_drive in mcp.connector.sync ([#252](https://github.com/ozzy-labs/opshub/issues/252)) ([3ca4edf](https://github.com/ozzy-labs/opshub/commit/3ca4edf331d687f96e653a300c3885d2f1ff5d5a))
* **connectors/box_drive:** office content extraction hook ([#247](https://github.com/ozzy-labs/opshub/issues/247)) ([0d15bb7](https://github.com/ozzy-labs/opshub/commit/0d15bb7ef3f51f530c14dcddf5dd082310ca0142))
* **connectors/ms365:** outlook body deep retention ([#244](https://github.com/ozzy-labs/opshub/issues/244)) ([0da7271](https://github.com/ozzy-labs/opshub/commit/0da7271a53ca3c18d6b1e467895573b95f922ac9))
* **connectors/onedrive_drive:** new local-fs connector ([#248](https://github.com/ozzy-labs/opshub/issues/248)) ([41e113c](https://github.com/ozzy-labs/opshub/commit/41e113ce63e90c3d143a52522443218bc0a0b5de))
* **connectors/teams:** graph chat delta + user token ([#243](https://github.com/ozzy-labs/opshub/issues/243)) ([cf1739d](https://github.com/ozzy-labs/opshub/commit/cf1739d5ab9b06ce382f19e950a22a8cc0cd57bc))
* **core:** office document extraction foundation ([#245](https://github.com/ozzy-labs/opshub/issues/245)) ([2845e8b](https://github.com/ozzy-labs/opshub/commit/2845e8bf5ea6585d01467fd68395f810e63f1018))
* **mcp,skills:** phase 12 h1 foundation (adr revisions + 4 new mcp tools + existing 5 rename) ([#262](https://github.com/ozzy-labs/opshub/issues/262)) ([178c4dc](https://github.com/ozzy-labs/opshub/commit/178c4dc83808f80c1cd2406b1e57ee6cdacfb5eb))
* **mcp:** widen tool surface (brief + graph + source + propose.generate) ([#231](https://github.com/ozzy-labs/opshub/issues/231)) ([451214c](https://github.com/ozzy-labs/opshub/commit/451214c0b9909d1cc3fe4e7bff4f7e4e59aa6fb0))
* **skills,mcp:** h4 hitl write (inbox-triage + source-extract + meeting-followup) ([#266](https://github.com/ozzy-labs/opshub/issues/266)) ([19ade66](https://github.com/ozzy-labs/opshub/commit/19ade66a42c5836ade8d5ac83846bc77c4ff0c98))
* **skills:** h2 info gathering (meeting-prep + research) ([#265](https://github.com/ozzy-labs/opshub/issues/265)) ([94fdee1](https://github.com/ozzy-labs/opshub/commit/94fdee17faca840e12a9b0896b523672248187e9))
* **skills:** h5 draft (handoff-draft + announcement-draft) ([#263](https://github.com/ozzy-labs/opshub/issues/263)) ([5fe43f4](https://github.com/ozzy-labs/opshub/commit/5fe43f409c23c4b515f1a7e411a7dcca705426b6))


### Fixed

* **ci:** post-Wave-2 CI hotfix (ruff format + mypy strict) ([#246](https://github.com/ozzy-labs/opshub/issues/246)) ([1cab7fd](https://github.com/ozzy-labs/opshub/commit/1cab7fdb45dd5fa0d97a4fd6b211df9dbd42fa79))
* **docs,skills:** phase 12 audit cluster A (catalog drift) ([#270](https://github.com/ozzy-labs/opshub/issues/270)) ([31dbfde](https://github.com/ozzy-labs/opshub/commit/31dbfdee2bf2ef224ff0465fea2a80fdc118dc01))
* **tests:** remove unused type: ignore in test_skill_specs ([#268](https://github.com/ozzy-labs/opshub/issues/268)) ([96af9be](https://github.com/ozzy-labs/opshub/commit/96af9bebff68efd3625ea8a5162a5cea63faf660))


### Documentation

* **adr:** adr-0025 office doc extraction / adr-0019 + 0010 改訂 (phase 11) ([#242](https://github.com/ozzy-labs/opshub/issues/242)) ([942b485](https://github.com/ozzy-labs/opshub/commit/942b485c2bfa20b07e74a3709c27c42b86ef5af2))
* **audit:** phase 11 audit cluster C (excludes flat + claudemd status + keyring slot + e2e gap) ([#251](https://github.com/ozzy-labs/opshub/issues/251)) ([c6f687c](https://github.com/ozzy-labs/opshub/commit/c6f687cd4ad5006ebc4d9bd817d1d50ad838bf80))
* phase 11 audit cluster A (skill drift fix) ([#250](https://github.com/ozzy-labs/opshub/issues/250)) ([bd16101](https://github.com/ozzy-labs/opshub/commit/bd16101a41b94def906b1380fcbd9c63ba17427b))
* phase 11 closeout (docs + e2e + guard) ([#249](https://github.com/ozzy-labs/opshub/issues/249)) ([9e1c8ea](https://github.com/ozzy-labs/opshub/commit/9e1c8eabbf6f2da90e455e8a1bba6b39c7a475fd))
* phase 12 audit cluster C (cross-cutting drift) ([#271](https://github.com/ozzy-labs/opshub/issues/271)) ([e2b1e8e](https://github.com/ozzy-labs/opshub/commit/e2b1e8e8f3f3dffbf75a2c82f1d3bd0e77bb44ff))
* phase 12 closeout + e2e lifecycle test ([#269](https://github.com/ozzy-labs/opshub/issues/269)) ([694fe74](https://github.com/ozzy-labs/opshub/commit/694fe74823ff5520fc93018f3d7cc20d2897af0f))
* **plan:** add Phase 11 implementation plan (MS Office 深掘り) ([#240](https://github.com/ozzy-labs/opshub/issues/240)) ([fc850fd](https://github.com/ozzy-labs/opshub/commit/fc850fd04b5732e8183014521eeb28515032397c))
* **plan:** add Phase 12 implementation plan (Secretary Skills 拡張) ([#260](https://github.com/ozzy-labs/opshub/issues/260)) ([7a4ebbd](https://github.com/ozzy-labs/opshub/commit/7a4ebbd6f1e8fcdec3b857b254fca473340930ae))
* **plan:** fix source_type=calendar_event → ms365_calendar in plan ([#273](https://github.com/ozzy-labs/opshub/issues/273)) ([65ec0ad](https://github.com/ozzy-labs/opshub/commit/65ec0ad0da50c86f4d408b09685c0910b8c927cc))
* **plan:** phase 11 pre-implementation audit updates ([#241](https://github.com/ozzy-labs/opshub/issues/241)) ([f9455b2](https://github.com/ozzy-labs/opshub/commit/f9455b2f8e0868af693a3f65681cbdbe5efd80fc))
* **plan:** phase 12 pre-implementation audit corrections ([#261](https://github.com/ozzy-labs/opshub/issues/261)) ([3c1db76](https://github.com/ozzy-labs/opshub/commit/3c1db765def7526b4c9031ca2e5d64b26ce11068))

## [0.2.3](https://github.com/ozzy-labs/opshub/compare/v0.2.2...v0.2.3) (2026-05-30)


### Added

* **connectors:** integrate excludes in ms365/box + body/provenance test coverage ([#226](https://github.com/ozzy-labs/opshub/issues/226)) ([a6e09ae](https://github.com/ozzy-labs/opshub/commit/a6e09aee2502d1bb3ee6b422cca7e44182f447b9))
* **mcp:** error-path redact + token shape expansion + pagination + test hardening ([#223](https://github.com/ozzy-labs/opshub/issues/223)) ([838501b](https://github.com/ozzy-labs/opshub/commit/838501bdba770ecd8ad65b62efe2302f6695bdda))
* **mcp:** stdio mcp server + tool schema ([#215](https://github.com/ozzy-labs/opshub/issues/215)) ([afc4288](https://github.com/ozzy-labs/opshub/commit/afc428876522e03985eb1dadd29b1158c0e541a3))
* **propose:** add reply-draft candidate kind ([#217](https://github.com/ozzy-labs/opshub/issues/217)) ([585faee](https://github.com/ozzy-labs/opshub/commit/585faee606061413eec5c467c6f5b81191ab734e))
* **search:** body-based embedding + sqlite fts5 + search command ([#214](https://github.com/ozzy-labs/opshub/issues/214)) ([0f2d624](https://github.com/ozzy-labs/opshub/commit/0f2d624e1f4fa57b6f22320a966a1f67197d2d28))
* **skills:** add secretary agent skill specs and security scan ([#219](https://github.com/ozzy-labs/opshub/issues/219)) ([dbef400](https://github.com/ozzy-labs/opshub/commit/dbef4007c812bfdf1ff08df8cbb30fc8d67589dc))
* store full content locally with at-rest encryption and excludes ([#211](https://github.com/ozzy-labs/opshub/issues/211)) ([47cf539](https://github.com/ozzy-labs/opshub/commit/47cf53965b1e5996462e047d9293e292325d811b))


### Fixed

* **ci:** mypy strict (types stubs + decorator ignores) after phase 10 audit ([#227](https://github.com/ozzy-labs/opshub/issues/227)) ([a8460c1](https://github.com/ozzy-labs/opshub/commit/a8460c1043c37df5496d19ce8a40c0bc8a936a80))
* **connectors/github:** defer httpx import so slack sync works without github extra ([#200](https://github.com/ozzy-labs/opshub/issues/200)) ([c00c0c7](https://github.com/ozzy-labs/opshub/commit/c00c0c7940ab82085a488bb5304bd4f6ddb1f9fb)), closes [#198](https://github.com/ozzy-labs/opshub/issues/198)
* **skills,adr:** align skill MCP calls with schema + clarify triage as generate-time hint ([#228](https://github.com/ozzy-labs/opshub/issues/228)) ([0a5bb38](https://github.com/ozzy-labs/opshub/commit/0a5bb386399aba42fdc41f982127a6b2efd41540))
* **tests:** rebuild secret-shaped test fixtures from parts to silence scanners ([#230](https://github.com/ozzy-labs/opshub/issues/230)) ([b40cd29](https://github.com/ozzy-labs/opshub/commit/b40cd2907038a9b4b05177f68a622f66a47dfeca))


### Documentation

* **adr:** adr-0012 revise embed target to body (phase 10) ([#212](https://github.com/ozzy-labs/opshub/issues/212)) ([d6d2528](https://github.com/ozzy-labs/opshub/commit/d6d252889b859a30ec33a3afc906f0dd20d021d4))
* **adr:** adr-0020 full local content retention / adr-0021 encryption at rest ([#210](https://github.com/ozzy-labs/opshub/issues/210)) ([18d6a8e](https://github.com/ozzy-labs/opshub/commit/18d6a8ed8df47f480a74277b2e3145410ba72a52))
* **adr:** adr-0022 mcp server surface ([#213](https://github.com/ozzy-labs/opshub/issues/213)) ([e26d2b0](https://github.com/ozzy-labs/opshub/commit/e26d2b03ebb915d3ce51d435425b07ac7a153722))
* **adr:** revise adr-0004 for phase 10 form-a absorption ([#218](https://github.com/ozzy-labs/opshub/issues/218)) ([5edfca5](https://github.com/ozzy-labs/opshub/commit/5edfca5121602075b647e3a685b36d5de0371eab))
* **adr:** revise adr-0016/0017/0010 for phase 10 reply-draft ([#216](https://github.com/ozzy-labs/opshub/issues/216)) ([8f05a6f](https://github.com/ozzy-labs/opshub/commit/8f05a6f4d0a1b62293f6c369b5ae487f07f31b09))
* align skill / README / secretary docs with actual CLI surface ([#222](https://github.com/ozzy-labs/opshub/issues/222)) ([874acf2](https://github.com/ozzy-labs/opshub/commit/874acf232be3c0f3e21401aa77899605486adc5c))
* phase 10 audit alignment (adr + principles + architecture + extras cleanup) ([#225](https://github.com/ozzy-labs/opshub/issues/225)) ([1086791](https://github.com/ozzy-labs/opshub/commit/1086791a38f0749eba2fd9e8a2c3b844ef32e36a))
* **plan:** add Phase 10 secretary-agent platform plan ([#202](https://github.com/ozzy-labs/opshub/issues/202)) ([c64d2d4](https://github.com/ozzy-labs/opshub/commit/c64d2d49a666c62ee78696ef908d55417135e038))

## [0.2.2](https://github.com/ozzy-labs/opshub/compare/v0.2.1...v0.2.2) (2026-05-22)


### Added

* **cli:** box_drive sync + phase 9 closeout ([#194](https://github.com/ozzy-labs/opshub/issues/194)) ([3d97643](https://github.com/ozzy-labs/opshub/commit/3d976431e27a14da7a4d3811044e396d2ddf6f77))
* **connectors/box_drive:** mapper + connector + settings ([#193](https://github.com/ozzy-labs/opshub/issues/193)) ([90d3f49](https://github.com/ozzy-labs/opshub/commit/90d3f49d680a2bd6d16aef5ad749c1f084b344a2))
* **core:** platform detection + box_drive scanner ([#191](https://github.com/ozzy-labs/opshub/issues/191)) ([ca4e1da](https://github.com/ozzy-labs/opshub/commit/ca4e1dadb722077712634feec57a6c4437e50103))
* **sources:** add fingerprint column + migration 0017 ([#192](https://github.com/ozzy-labs/opshub/issues/192)) ([97929ed](https://github.com/ozzy-labs/opshub/commit/97929ed090fc8b0e6a4bd5e880b6fd677af26010))


### Documentation

* **adr:** adr-0019 local-filesystem-backed connector ([#190](https://github.com/ozzy-labs/opshub/issues/190)) ([8ff9c1a](https://github.com/ozzy-labs/opshub/commit/8ff9c1a416e686971f51709830c7b0429394e534))
* **plan:** align phase 9 plan A1 row to actual adr-0019 ([#195](https://github.com/ozzy-labs/opshub/issues/195)) ([da353ef](https://github.com/ozzy-labs/opshub/commit/da353efc5e04e21db169cf0f260a005164167e8c))
* **plan:** phase 9 plan ([#188](https://github.com/ozzy-labs/opshub/issues/188)) ([613a2f3](https://github.com/ozzy-labs/opshub/commit/613a2f3dcfcbb3d41ea4fdd1f65768230b1f4e0f))

## [0.2.1](https://github.com/ozzy-labs/opshub/compare/v0.2.0...v0.2.1) (2026-05-19)


### Fixed

* **release-please:** refresh uv.lock and auto-sync on future release PRs ([#181](https://github.com/ozzy-labs/opshub/issues/181)) ([e2d7544](https://github.com/ozzy-labs/opshub/commit/e2d7544b4d744603093291662fa2727f6f2ee1a2))
* **tests:** assert __version__ matches SemVer shape instead of hardcoded value ([#182](https://github.com/ozzy-labs/opshub/issues/182)) ([6d0e696](https://github.com/ozzy-labs/opshub/commit/6d0e69667b5c834383c5ff48b8c3a717afdacb1a))


### Documentation

* **release-runbook:** correct primary workflow name for PyPI Trusted Publisher ([#179](https://github.com/ozzy-labs/opshub/issues/179)) ([c1cfc3e](https://github.com/ozzy-labs/opshub/commit/c1cfc3ef9826a61c53b187067d130792af8634d6))

## [0.2.0](https://github.com/ozzy-labs/opshub/compare/v0.1.1...v0.2.0) (2026-05-18)


### ⚠ BREAKING CHANGES

* **connectors/slack:** make User Token the first-class principal ([#167](https://github.com/ozzy-labs/opshub/issues/167))

### Added

* **cli:** add generic `opshub connector auth test <name>` subcommand ([#176](https://github.com/ozzy-labs/opshub/issues/176)) ([d4eb986](https://github.com/ozzy-labs/opshub/commit/d4eb9866f7792d296dd788e47e17edfe52bfe1ef))
* **connectors/slack:** include scope-extension hint in missing_scope error message ([#172](https://github.com/ozzy-labs/opshub/issues/172)) ([7338ae3](https://github.com/ozzy-labs/opshub/commit/7338ae3baa61e2b861abe7e72ed17c3cbb7ad29c)), closes [#169](https://github.com/ozzy-labs/opshub/issues/169)
* **connectors/slack:** make User Token the first-class principal ([#167](https://github.com/ozzy-labs/opshub/issues/167)) ([10a01ac](https://github.com/ozzy-labs/opshub/commit/10a01acac55014d279584dd9dce742b9792bcfd8))


### Changed

* **connectors/slack:** make principal detection defensive against bot_id falsy values ([#171](https://github.com/ozzy-labs/opshub/issues/171)) ([9dfe8bb](https://github.com/ozzy-labs/opshub/commit/9dfe8bb339591453fb0e07a9abac55427c98dc4d)), closes [#168](https://github.com/ozzy-labs/opshub/issues/168)
* **connectors:** align auth-test polish (httpx DI, dispatch table, config pointers) ([#177](https://github.com/ozzy-labs/opshub/issues/177)) ([aa05fc6](https://github.com/ozzy-labs/opshub/commit/aa05fc674689d4c18a515ae0fdc947b1e2b3809f))

## [0.1.1](https://github.com/ozzy-labs/opshub/compare/v0.1.0...v0.1.1) (2026-05-17)


### Documentation

* **readme:** split into README.md (en) + README.ja.md (ja) with cross-links ([#161](https://github.com/ozzy-labs/opshub/issues/161)) ([30cd55c](https://github.com/ozzy-labs/opshub/commit/30cd55c4016253bd5d8513a6af819f00f71f0e63))

## [0.1.0] - 2026-05-17

Initial public release. OpsHub is a local-first operational memory + execution
hub for humans and AI agents.

**Distribution**: v0.1.0 ships on PyPI under the distribution name
**`ozzylabs-opshub`** (PEP 423 `<owner>-<package>` form because PyPI has no
namespace concept and the bare `opshub` name was unavailable). The CLI
command remains `opshub`. Install via `uv tool install ozzylabs-opshub` or
directly from a tag at `git+https://github.com/ozzy-labs/opshub.git@v0.1.0`.
See [ADR-0001 §Updates](docs/adr/0001-python-stack.md#updates) for naming
rationale.

### Added

#### Phase 1 — Foundation (event store + CLI + markdown)

- Event-sourced architecture (ADR-0002): all state changes via `events` table,
  projections rebuildable
- Core projections: `tasks` / `events`
- CLI: `opshub init` / `opshub task add|list|update|complete|delete` /
  `opshub db migrate` / `opshub projections rebuild`
- Markdown generation for workspace surface (ADR-0003)
- Pluggable Embedder + VectorStore Protocols frozen for Phase 4 (ADR-0012)

#### Phase 2 — Coordination

- Projections: `inbox_items` / `decisions` / `work_sessions` / `agent_runs` /
  `locks` / `handoffs`
- CLI: `opshub inbox` / `opshub decision` / `opshub session` /
  `opshub agent run` / `opshub handoff` / `opshub lock`
- Lock granularity (ADR-0013): `task:<id>` / `project:<id>` / `global:` 3-tier
  scope

#### Phase 3 — Connector framework + Workspace ingest

- Connector framework (ADR-0010 contract): `Connector` Protocol +
  `connector_cursors` projection
- GitHub connector (issues + PRs + notifications)
- SaaS token storage (ADR-0014): `core/secrets` + keyring + env var override
- CLI: `opshub connector` (auth set / sync / list) + `opshub workspace`
  (ingest / generate)
- Projections: `sources` / `connector_cursors` / `ingested_files`

#### Phase 4 — Semantic recall

- Pluggable Embedder backends: local (bge-m3) / OpenAI / Voyage
- sqlite-vec VectorStore (3 backend-specific vec0 tables via migration 0013)
- `EmbeddingService` (CLI-driven rebuild) + `RecallService` + `DuplicateService`
- CLI: `opshub embeddings` (rebuild / status / find-duplicates) +
  `opshub recall`
- `VectorStore.recall_by_rowid` Protocol extension (follow-up: cost-effective
  duplicate detection)

#### Phase 5 — Briefing

- LLM usage strategy (ADR-0015): Pluggable LLM Protocol + default `disabled` +
  paste-code OAuth
- Pluggable LLM backends: Anthropic (claude-haiku-4-5) + OpenAI (gpt-4o-mini)
- `BriefingService` + `briefings` projection (migration 0014)
- Prompt injection mitigation: `<source>` delimiter wrap + `html.escape`
  (follow-up)
- CLI: `opshub brief` + `opshub embeddings drain`
- Event-driven auto-embed (opt-in `[embedding] auto = true`)

#### Phase 6 — Action loop

- Action loop / structured output (ADR-0016): tool_use / function calling per
  backend
- `LLMClient.complete_structured` Protocol extension
- Pluggable LLM backend: Ollama (local; closes ADR-0015 Local LLM deferred)
- `ProposalService` + `proposals` projection (migration 0015)
- CLI: `opshub propose` (generate / list / apply / reject)
- Human-in-the-loop enforced: no auto-apply mode (ADR-0004 alignment)

#### Phase 7 — Connectors Wave 2

- Slack connector (channels messages + permalink)
- Microsoft 365 connector (Calendar + OneDrive + Outlook via OAuth paste-code)
- Box connector (events API via OAuth paste-code)
- `connector:slack` CLI alias for backward-compat with `slack` (follow-up)

#### Phase 8 — Knowledge graph

- Knowledge graph (ADR-0017): `links` projection (migration 0016) + 4
  auto-extraction paths
- Manual link CRUD: `LinkCreated` / `LinkDeleted` events
- `LinkService` (related / trace / expand) with cycle detection + depth limits
- CLI: `opshub link` (add / remove / list) + `opshub graph`
  (related / trace / expand)
- `--expand-graph` integration on `opshub brief` and `opshub propose generate`

### Security

- API tokens stored via OS keyring (ADR-0014), env var override (`OPSHUB_*`)
- All event error_messages sanitised via
  `core.sanitise.sanitise_error_message` (sk-... / ghp_... / Bearer ...
  redacted)
- LLM prompt injection mitigation: external content delimiter-wrapped +
  HTML-escaped before LLM submission
- ADR-0005 External Content Min: source body never persisted (summary ≤ 200
  chars cap, enforced per connector)

### Performance

- Cold-start budget: `opshub --help` ≤ 300ms (measured ~140ms)
- Tests: 1533 passing + 9 skipped (extras-gated for optional ML / connector
  SDKs)

### Fixed

- **`opshub init` blocker on default install** — Phase 4 migration 0013
  unconditionally creates `embeddings_vec_*` virtual tables via `sqlite-vec`.
  Previously gated by the `[vector]` extras, so `uv tool install opshub` (the
  documented Quickstart) hit `OperationalError: no such module: vec0` and left
  the DB half-applied. Promoted `sqlite-vec` to base dependency (~500 KB wheel,
  within ADR-0001 distribution budget). `[vector]` extras remains as a
  `numpy`-only alias for backward compat — existing `uv sync --extra vector`
  invocations continue to work. See ADR-0001 §Updates for the rationale.

### Architecture

- 17 ADRs accepted (0000-0017). See `docs/adr/`.
- Event-sourced single source of truth (ADR-0002). All projections derivable
  from event log via `opshub projections rebuild`.
- Single Python package (ADR-0007). ML / LLM / connector SDKs in extras
  (ADR-0001 distribution constraint).
- Multi-Agent Neutrality (ADR-0009): Pluggable Protocols for Embedder /
  VectorStore / LLMClient + 3 backends each.

[0.1.0]: https://github.com/ozzy-labs/opshub/releases/tag/v0.1.0
