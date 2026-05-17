# Changelog

All notable changes to OpsHub will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.1](https://github.com/ozzy-labs/opshub/compare/v0.1.0...v0.1.1) (2026-05-17)


### Added

* **cli:** add --version flag to root app ([#152](https://github.com/ozzy-labs/opshub/issues/152)) ([760d502](https://github.com/ozzy-labs/opshub/commit/760d502097116a492ab99c9cd31cc670142e5f1c))
* **cli:** bootstrap commands (init, db migrate) ([#14](https://github.com/ozzy-labs/opshub/issues/14)) ([a2bbfb7](https://github.com/ozzy-labs/opshub/commit/a2bbfb77051120691957a13fc58c6b9ed974d8bc))
* **cli:** brief command ([#92](https://github.com/ozzy-labs/opshub/issues/92)) ([7fad3c6](https://github.com/ozzy-labs/opshub/commit/7fad3c691481d5814f2b13e15753e88a0e8c1c32))
* **cli:** embeddings drain command + status extension ([#88](https://github.com/ozzy-labs/opshub/issues/88)) ([f45826e](https://github.com/ozzy-labs/opshub/commit/f45826e8934605967800b567959e9dcb4e3d5968))
* **cli:** embeddings rebuild and status ([#71](https://github.com/ozzy-labs/opshub/issues/71)) ([eea1827](https://github.com/ozzy-labs/opshub/commit/eea18274c4f43a0fd0590c0f7e88cc825b88a909))
* **cli:** link + graph commands ([#140](https://github.com/ozzy-labs/opshub/issues/140)) ([ba90020](https://github.com/ozzy-labs/opshub/commit/ba90020958edecdb8caf15c307fbb9c3b3c45bb9))
* **cli:** ops commands (projections, embeddings) ([#16](https://github.com/ozzy-labs/opshub/issues/16)) ([f340fb2](https://github.com/ozzy-labs/opshub/commit/f340fb2374e435290fe5c29df59aae1669f5de49)), closes [#3](https://github.com/ozzy-labs/opshub/issues/3)
* **cli:** propose command ([#112](https://github.com/ozzy-labs/opshub/issues/112)) ([1cfce6b](https://github.com/ozzy-labs/opshub/commit/1cfce6b01c0159cd2fd81204f08de65e5052e9ef))
* **cli:** recall command ([#73](https://github.com/ozzy-labs/opshub/issues/73)) ([ee311db](https://github.com/ozzy-labs/opshub/commit/ee311db71e756479cb1e40065fe3ca29d92f67b1))
* **cli:** task commands (create, list) ([#19](https://github.com/ozzy-labs/opshub/issues/19)) ([854e958](https://github.com/ozzy-labs/opshub/commit/854e95885480effa81c752a02d28ac7f23920dbe))
* **cli:** workspace ingest command ([#54](https://github.com/ozzy-labs/opshub/issues/54)) ([b9ccc74](https://github.com/ozzy-labs/opshub/commit/b9ccc744e0555f2d937c5f72b7a7557e0d1ad841))
* **config:** embedding backend resolution + factory ([#68](https://github.com/ozzy-labs/opshub/issues/68)) ([3c9131e](https://github.com/ozzy-labs/opshub/commit/3c9131e27e8e4f5c38b9909a5aae0ec5d9da6166))
* **config:** llm backend resolution + factory ([#90](https://github.com/ozzy-labs/opshub/issues/90)) ([d3ba23a](https://github.com/ozzy-labs/opshub/commit/d3ba23a5a700ac6147633b48c9884fd13ea1531a))
* **config:** settings for storage/workspace/embedding ([#8](https://github.com/ozzy-labs/opshub/issues/8)) ([64a34a5](https://github.com/ozzy-labs/opshub/commit/64a34a507fad1f7fd5f8c9b6de25ef92e0f0dbd0))
* **connectors-github:** auth resolution ([#51](https://github.com/ozzy-labs/opshub/issues/51)) ([02d5ab9](https://github.com/ozzy-labs/opshub/commit/02d5ab9b13f01ad7e3b0f4062e0f089b3128a8dd))
* **connectors-github:** fetch primitives ([#52](https://github.com/ozzy-labs/opshub/issues/52)) ([f81dd86](https://github.com/ozzy-labs/opshub/commit/f81dd86a414f4bf3e77a73cdbe3b1ccda1e97070))
* **connectors/box:** auth + oauth flow + extras ([#118](https://github.com/ozzy-labs/opshub/issues/118)) ([1273dc5](https://github.com/ozzy-labs/opshub/commit/1273dc5a8a61c4fbbcb840bc2db06c41e39a9696))
* **connectors/box:** fetcher ([#119](https://github.com/ozzy-labs/opshub/issues/119)) ([52f6350](https://github.com/ozzy-labs/opshub/commit/52f6350befa77159c44768436d34940a667e2da8))
* **connectors/box:** mapper + sync integration ([#129](https://github.com/ozzy-labs/opshub/issues/129)) ([1215ba0](https://github.com/ozzy-labs/opshub/commit/1215ba0ceec7e789cf3715f724d66e3bcad027f6))
* **connectors/ms365:** auth + oauth flow + extras ([#116](https://github.com/ozzy-labs/opshub/issues/116)) ([a4a41eb](https://github.com/ozzy-labs/opshub/commit/a4a41ebde95b24858530ac867e2faa1cc3c66984))
* **connectors/ms365:** fetcher ([#120](https://github.com/ozzy-labs/opshub/issues/120)) ([47284aa](https://github.com/ozzy-labs/opshub/commit/47284aafbe6c8a32528e58abe75444dfd6705045))
* **connectors/ms365:** mapper + sync integration ([#130](https://github.com/ozzy-labs/opshub/issues/130)) ([908abfb](https://github.com/ozzy-labs/opshub/commit/908abfbfc138e959ac1b7d385ac51bea177e6a17))
* **connectors/slack:** auth + extras ([#115](https://github.com/ozzy-labs/opshub/issues/115)) ([7bd2cfb](https://github.com/ozzy-labs/opshub/commit/7bd2cfbdae49f3184da0b50da07ec0fb7a3d0852))
* **connectors/slack:** fetcher ([#121](https://github.com/ozzy-labs/opshub/issues/121)) ([95b73eb](https://github.com/ozzy-labs/opshub/commit/95b73ebcbf4b9d547909a37a183ee86c10edbc9e))
* **connectors/slack:** mapper + sync integration ([#131](https://github.com/ozzy-labs/opshub/issues/131)) ([0473a4a](https://github.com/ozzy-labs/opshub/commit/0473a4a45e6b152f0546aafd1f262691a7ef1b56))
* **connectors:** base protocol and registry ([#48](https://github.com/ozzy-labs/opshub/issues/48)) ([67d63bc](https://github.com/ozzy-labs/opshub/commit/67d63bc617e91971f70df44df3740ff394b99ec3))
* **coordination:** decisions workflow ([#32](https://github.com/ozzy-labs/opshub/issues/32)) ([b2fe4ec](https://github.com/ozzy-labs/opshub/commit/b2fe4ec83b311f87c9208543c4cd5252d564e1cb))
* **coordination:** handoffs workflow ([#33](https://github.com/ozzy-labs/opshub/issues/33)) ([23fe5e8](https://github.com/ozzy-labs/opshub/commit/23fe5e8884cd49a8eab4a503490cffd4f91cf1fb))
* **coordination:** inbox triage workflow ([#30](https://github.com/ozzy-labs/opshub/issues/30)) ([2ee6233](https://github.com/ozzy-labs/opshub/commit/2ee6233150012bb1010e5fdd320bb6893a63ccc1))
* **coordination:** lock implementation ([#31](https://github.com/ozzy-labs/opshub/issues/31)) ([56e1656](https://github.com/ozzy-labs/opshub/commit/56e1656c134624d0102c66355fe0664e777c788d))
* **coordination:** work sessions and agent runs ([#35](https://github.com/ozzy-labs/opshub/issues/35)) ([e87a139](https://github.com/ozzy-labs/opshub/commit/e87a1392b3687505ea5543e741d175c4daee6cf6))
* **core:** foundational utilities ([#6](https://github.com/ozzy-labs/opshub/issues/6)) ([e46a741](https://github.com/ozzy-labs/opshub/commit/e46a7416151d2a12aeacfac5c8e95c26aae38a7b))
* **core:** secrets storage + ADR-0014 ([#49](https://github.com/ozzy-labs/opshub/issues/49)) ([06207b2](https://github.com/ozzy-labs/opshub/commit/06207b22c943a4aeb9015b05fda340e0e86f65ef))
* **db:** enable sqlite extension loading + embeddings_vec migration ([#64](https://github.com/ozzy-labs/opshub/issues/64)) ([b2a4f82](https://github.com/ozzy-labs/opshub/commit/b2a4f82db8f68ee9a61a2dcbea09846d56fc8603))
* **db:** initial migration with events and embeddings tables ([#12](https://github.com/ozzy-labs/opshub/issues/12)) ([64005d9](https://github.com/ozzy-labs/opshub/commit/64005d912532629ec05857e07e9bd4f050b22faf))
* **db:** phase 2 projection tables ([#29](https://github.com/ozzy-labs/opshub/issues/29)) ([1321e3e](https://github.com/ozzy-labs/opshub/commit/1321e3e5e203124bb80883392e3d9b893f40478d))
* **db:** set up sqlalchemy engine and alembic ([#11](https://github.com/ozzy-labs/opshub/issues/11)) ([508bacb](https://github.com/ozzy-labs/opshub/commit/508bacbeb46218619147e17ef1fc05cb7402ca14))
* **db:** sources and connector_cursors migrations ([#45](https://github.com/ozzy-labs/opshub/issues/45)) ([75f7e22](https://github.com/ozzy-labs/opshub/commit/75f7e223efa295d043c1549d358a2a2dad1b0da6))
* **domain:** briefing events ([#84](https://github.com/ozzy-labs/opshub/issues/84)) ([63aab3c](https://github.com/ozzy-labs/opshub/commit/63aab3c91cc13a7514ab5e7a728b55c763e62d14))
* **domain:** embedding events ([#63](https://github.com/ozzy-labs/opshub/issues/63)) ([8279948](https://github.com/ozzy-labs/opshub/commit/8279948858f251c8495bce3d6e8d20abc0c86cf8))
* **domain:** link events + sourcereferenced closeout ([#135](https://github.com/ozzy-labs/opshub/issues/135)) ([236564a](https://github.com/ozzy-labs/opshub/commit/236564acc235dbe123da444bcfb53309ea398082))
* **domain:** phase 2 events ([#28](https://github.com/ozzy-labs/opshub/issues/28)) ([2cfa586](https://github.com/ozzy-labs/opshub/commit/2cfa5868d49875ade2c7103c60e3a4795d0a559d))
* **domain:** proposal events ([#101](https://github.com/ozzy-labs/opshub/issues/101)) ([4bef6cc](https://github.com/ozzy-labs/opshub/commit/4bef6cc7eac8c6fc144a5a0ba5a8cda4cfe48007))
* **domain:** source and connector events ([#44](https://github.com/ozzy-labs/opshub/issues/44)) ([1af8d4e](https://github.com/ozzy-labs/opshub/commit/1af8d4e613dbd2545334683520d210e71168f9a5))
* **domain:** task events ([#10](https://github.com/ozzy-labs/opshub/issues/10)) ([9007cc6](https://github.com/ozzy-labs/opshub/commit/9007cc60ea36af0deb5af74a735ff684163c4a64))
* **llm:** AnthropicLLMClient ([#86](https://github.com/ozzy-labs/opshub/issues/86)) ([1deaf80](https://github.com/ozzy-labs/opshub/commit/1deaf80e2b2cfd64d6cdc917eddca76537d77fbc))
* **llm:** extend LLMClient with complete_structured ([#102](https://github.com/ozzy-labs/opshub/issues/102)) ([58a76bf](https://github.com/ozzy-labs/opshub/commit/58a76bf20006c7eee219f4c9bfc71ecd58c58a86))
* **llm:** LLMClient Protocol + freeze test ([#83](https://github.com/ozzy-labs/opshub/issues/83)) ([64fdc7a](https://github.com/ozzy-labs/opshub/commit/64fdc7aedc7e5f7611bdf7081e9afc40a9bdcac8))
* **llm:** OllamaLLMClient (local backend) ([#105](https://github.com/ozzy-labs/opshub/issues/105)) ([aff5325](https://github.com/ozzy-labs/opshub/commit/aff532558cb988e2075da1c820fe56816339606a))
* **llm:** OpenAILLMClient ([#87](https://github.com/ozzy-labs/opshub/issues/87)) ([bfdbabb](https://github.com/ozzy-labs/opshub/commit/bfdbabbf70b1f13607df07a8d209d82244b663df))
* **llm:** structured output for Anthropic + OpenAI ([#103](https://github.com/ozzy-labs/opshub/issues/103)) ([f245b2a](https://github.com/ozzy-labs/opshub/commit/f245b2a9596d77d135f1b819d87dae363b849761))
* **markdown:** inbox ingest parser ([#50](https://github.com/ozzy-labs/opshub/issues/50)) ([6a36049](https://github.com/ozzy-labs/opshub/commit/6a360498aaea467fe7a9ae85202be969386a1a9c))
* **markdown:** inbox/decisions/handoffs rendering ([#34](https://github.com/ozzy-labs/opshub/issues/34)) ([d732c67](https://github.com/ozzy-labs/opshub/commit/d732c6741b94862bb73eb6e986d74ffd2a725883))
* **markdown:** task list rendering to workspace ([#17](https://github.com/ozzy-labs/opshub/issues/17)) ([7967192](https://github.com/ozzy-labs/opshub/commit/7967192d1ca949621d9bf71bed44aea5f0437b70))
* **projections:** briefings projection + migration 0014 ([#89](https://github.com/ozzy-labs/opshub/issues/89)) ([2deed98](https://github.com/ozzy-labs/opshub/commit/2deed98a31a4849d76cdba32699a160698d309b7))
* **projections:** links projection + migration 0016 ([#136](https://github.com/ozzy-labs/opshub/issues/136)) ([0ed1e29](https://github.com/ozzy-labs/opshub/commit/0ed1e2907085e141f1e57390e0f28a0095605388))
* **projections:** LinksExtractor projector ([#138](https://github.com/ozzy-labs/opshub/issues/138)) ([48397e0](https://github.com/ozzy-labs/opshub/commit/48397e04c3f3f959319ad565663dbfda198994f0))
* **projections:** proposals projection + migration 0015 ([#104](https://github.com/ozzy-labs/opshub/issues/104)) ([21a1669](https://github.com/ozzy-labs/opshub/commit/21a16698100e3d88d1a3901198eb0e0c47b93bd1))
* **projections:** sources and connector_cursors ([#46](https://github.com/ozzy-labs/opshub/issues/46)) ([a89887a](https://github.com/ozzy-labs/opshub/commit/a89887a2d1934f402bdf6636eac531d526c204fe))
* **projections:** tasks projection + replay test ([#15](https://github.com/ozzy-labs/opshub/issues/15)) ([7c2eba9](https://github.com/ozzy-labs/opshub/commit/7c2eba939f160d03e9629e0e506c59339b1010d7))
* **services:** auto-embed projector hook ([#85](https://github.com/ozzy-labs/opshub/issues/85)) ([e39a97a](https://github.com/ozzy-labs/opshub/commit/e39a97a3b4aa87d8ce0311513db261dae792645f))
* **services:** briefing service ([#91](https://github.com/ozzy-labs/opshub/issues/91)) ([7d459f2](https://github.com/ozzy-labs/opshub/commit/7d459f29086b63b4c12ca43f47e6d60a06902cc6))
* **services:** briefing/propose --expand-graph + graph expand CLI wiring ([#141](https://github.com/ozzy-labs/opshub/issues/141)) ([56c3758](https://github.com/ozzy-labs/opshub/commit/56c37584e14b4f151bdc16be5fe0a77ea4d9b060))
* **services:** duplicate detection ([#72](https://github.com/ozzy-labs/opshub/issues/72)) ([9d2377b](https://github.com/ozzy-labs/opshub/commit/9d2377bc24f6ebd38a5230b531d4059c2058311d))
* **services:** embedding service (CLI-driven rebuild) ([#69](https://github.com/ozzy-labs/opshub/issues/69)) ([918fef2](https://github.com/ozzy-labs/opshub/commit/918fef22df27979d35a74141d021b3e97ecfeef8))
* **services:** LinkService expand + bidirectional ([#139](https://github.com/ozzy-labs/opshub/issues/139)) ([8c89de1](https://github.com/ozzy-labs/opshub/commit/8c89de196e70f3b2c89e3836a382f2029b5667d3))
* **services:** LinkService related + trace ([#137](https://github.com/ozzy-labs/opshub/issues/137)) ([32ca894](https://github.com/ozzy-labs/opshub/commit/32ca894a6f40f5ca2f8657f70e11e57a71557d72))
* **services:** proposal service ([#106](https://github.com/ozzy-labs/opshub/issues/106)) ([aa75c67](https://github.com/ozzy-labs/opshub/commit/aa75c67ec8cf0cb1e56f884d4ffffa61690cf8dd))
* **services:** recall service ([#70](https://github.com/ozzy-labs/opshub/issues/70)) ([e71ba21](https://github.com/ozzy-labs/opshub/commit/e71ba2135bc332a8921c5368442d036f62459a90))
* **services:** source service ([#47](https://github.com/ozzy-labs/opshub/issues/47)) ([68c14e5](https://github.com/ozzy-labs/opshub/commit/68c14e5ee6813b714981dbba5498e5e787074dde))
* **services:** task service ([#13](https://github.com/ozzy-labs/opshub/issues/13)) ([435a190](https://github.com/ozzy-labs/opshub/commit/435a19050ebc05dfc6770d09b4cbad0b2d33c220))
* **vectors:** add recall_by_rowid to VectorStore Protocol ([#75](https://github.com/ozzy-labs/opshub/issues/75)) ([bb59e51](https://github.com/ozzy-labs/opshub/commit/bb59e518f365cc8e0968af24a6c1c364aac650d0))
* **vectors:** LocalSentenceTransformerEmbedder ([#66](https://github.com/ozzy-labs/opshub/issues/66)) ([c47ed44](https://github.com/ozzy-labs/opshub/commit/c47ed442590982e092d132877793dcf448e043d4))
* **vectors:** OpenAI + Voyage API embedders ([#65](https://github.com/ozzy-labs/opshub/issues/65)) ([4606d2d](https://github.com/ozzy-labs/opshub/commit/4606d2d6659cba50adcfec8da924b773ed4b0ef7))
* **vectors:** pluggable embedder/store protocols ([#9](https://github.com/ozzy-labs/opshub/issues/9)) ([bfab556](https://github.com/ozzy-labs/opshub/commit/bfab556952edb03960b4d72868cb8418f24e76fa))
* **vectors:** SqliteVecStore implementation ([#67](https://github.com/ozzy-labs/opshub/issues/67)) ([96c1c93](https://github.com/ozzy-labs/opshub/commit/96c1c9350297b7e2471138a0cbc61d69bc4beac8))
* **workspace:** file ingest service + state tracking ([#53](https://github.com/ozzy-labs/opshub/issues/53)) ([31f7035](https://github.com/ozzy-labs/opshub/commit/31f7035f0762d9e5d92857f59ea34f6fc2dd53f1))


### Fixed

* **briefings:** escape source body to harden injection mitigation ([#94](https://github.com/ozzy-labs/opshub/issues/94)) ([d717097](https://github.com/ozzy-labs/opshub/commit/d717097c3b800150aedb2bede4366819e8972ced))
* **cli:** accept connector:slack as alias for slack ([#133](https://github.com/ozzy-labs/opshub/issues/133)) ([e14e406](https://github.com/ozzy-labs/opshub/commit/e14e406dc3fc782eeff756fe237ab4a6fb006661))
* **connectors/github:** enforce ADR-0005 summary length cap ([#147](https://github.com/ozzy-labs/opshub/issues/147)) ([28b7089](https://github.com/ozzy-labs/opshub/commit/28b70899838a57931d3f40ee5d7a8fe169e8c209))
* **coordination:** lock reacquire fails because SQLite returns naive datetimes ([#37](https://github.com/ozzy-labs/opshub/issues/37)) ([67239f3](https://github.com/ozzy-labs/opshub/commit/67239f3849933d3cf45c8e933a0f55a7873b659b))
* **core:** reject ULID strings that overflow 128 bits ([#7](https://github.com/ozzy-labs/opshub/issues/7)) ([a23031f](https://github.com/ozzy-labs/opshub/commit/a23031fb70b80dc7605dddc5ba5c6e572400a24c))
* **deps:** promote sqlite-vec to base dependency to unblock opshub init ([#156](https://github.com/ozzy-labs/opshub/issues/156)) ([8f4884e](https://github.com/ozzy-labs/opshub/commit/8f4884edf2f2080b6bd1c23c19ce312e7f7ab485))


### Changed

* phase 2 prep — atomic transaction, projection registry, AllEvent ([#26](https://github.com/ozzy-labs/opshub/issues/26)) ([4f6d303](https://github.com/ozzy-labs/opshub/commit/4f6d30310d4249a479d9e988fc7cf6955dd21196))


### Documentation

* add phase 2 implementation plan ([#22](https://github.com/ozzy-labs/opshub/issues/22)) ([f37ba2a](https://github.com/ozzy-labs/opshub/commit/f37ba2ae4c0becb5c0d35ac920a69c5950823728))
* add phase 3 implementation plan ([#38](https://github.com/ozzy-labs/opshub/issues/38)) ([d41d37c](https://github.com/ozzy-labs/opshub/commit/d41d37c7938b81c2aabf31bedb2694131a2579f6))
* add phase 4 implementation plan ([#57](https://github.com/ozzy-labs/opshub/issues/57)) ([c19444f](https://github.com/ozzy-labs/opshub/commit/c19444f4c8d53a7d25335eeb02f9eba85fa72fc3))
* add upgrading + performance baseline guides ([#153](https://github.com/ozzy-labs/opshub/issues/153)) ([e9bac0a](https://github.com/ozzy-labs/opshub/commit/e9bac0aee14c8e36b552b709a139231a1f66bd7e))
* **adr:** add ADR-0013 Lock Granularity ([#24](https://github.com/ozzy-labs/opshub/issues/24)) ([9e33b9e](https://github.com/ozzy-labs/opshub/commit/9e33b9ea09542042d32795e7923587c66ac79d9d))
* **adr:** adr-0015 llm usage strategy ([#82](https://github.com/ozzy-labs/opshub/issues/82)) ([bfb2c22](https://github.com/ozzy-labs/opshub/commit/bfb2c22a9644b7bf8f743824ff75087ceb7e0b0c))
* **adr:** adr-0016 action loop and structured output ([#100](https://github.com/ozzy-labs/opshub/issues/100)) ([8b62133](https://github.com/ozzy-labs/opshub/commit/8b62133a2eb958d24977adee9b514a6ea7daf390))
* **adr:** adr-0017 knowledge graph ([#134](https://github.com/ozzy-labs/opshub/issues/134)) ([453452e](https://github.com/ozzy-labs/opshub/commit/453452e2acdd32c57efa6993b056dc1c7e211b34))
* **adr:** promote phase 1 foundational adrs to accepted ([#5](https://github.com/ozzy-labs/opshub/issues/5)) ([b9994ec](https://github.com/ozzy-labs/opshub/commit/b9994ec82fe47e9fbd87bec5357553571753295f)), closes [#3](https://github.com/ozzy-labs/opshub/issues/3)
* **adr:** refresh validation + open questions across phases ([#144](https://github.com/ozzy-labs/opshub/issues/144)) ([a155349](https://github.com/ozzy-labs/opshub/commit/a15534909b1af518078fbf602992668c4bc758eb))
* align all docs with phase 1 completion and ADR-0013 ([#25](https://github.com/ozzy-labs/opshub/issues/25)) ([34b32a2](https://github.com/ozzy-labs/opshub/commit/34b32a242c977e6771f3ef8e99ae3dcce08f35e5))
* align plan with merged plan-revision pr numbering ([#4](https://github.com/ozzy-labs/opshub/issues/4)) ([a172fbf](https://github.com/ozzy-labs/opshub/commit/a172fbf6df1b1a0a58f484122c4081c8f52b12af))
* phase 5 plan (briefing layer + llm adr) ([#76](https://github.com/ozzy-labs/opshub/issues/76)) ([2e8d4e1](https://github.com/ozzy-labs/opshub/commit/2e8d4e13bc4fb3e595a52cd4504a1d556932803b))
* phase 6 plan (action loop + local llm) ([#95](https://github.com/ozzy-labs/opshub/issues/95)) ([12fbee1](https://github.com/ozzy-labs/opshub/commit/12fbee1eda331abacb7dd43f15df3d67447c501f))
* phase 7 plan (connectors wave 2) ([#107](https://github.com/ozzy-labs/opshub/issues/107)) ([241e3d2](https://github.com/ozzy-labs/opshub/commit/241e3d2feb47a0851d54698368bfe52f89c8f9de))
* phase 8 plan (knowledge graph) ([#122](https://github.com/ozzy-labs/opshub/issues/122)) ([ef251ef](https://github.com/ozzy-labs/opshub/commit/ef251efe8cc445069e32fd51e15370393534e753))
* phase 8 README + cross-doc polish ([#145](https://github.com/ozzy-labs/opshub/issues/145)) ([e2d9e2c](https://github.com/ozzy-labs/opshub/commit/e2d9e2c2c2bf492b74ccc48c04ac73575902acfd))
* **phase-2:** revision 2 incorporating prep PRs and review findings ([#27](https://github.com/ozzy-labs/opshub/issues/27)) ([3ef8519](https://github.com/ozzy-labs/opshub/commit/3ef85198b08ffede8c1c62c323432e9222c2db71))
* **readme:** polish for v0.1.0 release ([#150](https://github.com/ozzy-labs/opshub/issues/150)) ([be3cfa9](https://github.com/ozzy-labs/opshub/commit/be3cfa9e61179a8bd249a843549ff106ac62d7a2))
* refine phase 1 plan with foundation layer and pr strategy ([#2](https://github.com/ozzy-labs/opshub/issues/2)) ([e11eea7](https://github.com/ozzy-labs/opshub/commit/e11eea7d102262acbf8ac5c64d383d71566d9365))
* **release:** v0.1.0 release notes + runbook ([#155](https://github.com/ozzy-labs/opshub/issues/155)) ([cc284c3](https://github.com/ozzy-labs/opshub/commit/cc284c3529320a2ca9a175651760d1fcbea537ca))
* **security:** expand security policy + supported versions ([#154](https://github.com/ozzy-labs/opshub/issues/154)) ([bd03561](https://github.com/ozzy-labs/opshub/commit/bd0356181f8b7f6350cd0cbd2ba401d0112ac54d))

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
