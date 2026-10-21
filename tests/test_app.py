import json
import shutil
import tempfile
import unittest
from pathlib import Path

import app


class TraceglassTests(unittest.TestCase):
    def test_exact_metadata_types_arrays_and_match_paths(self):
        path=self.write_export()
        export=json.loads(path.read_text())
        md={'content_references':[{'items':[{'attribution':'Past chat'},{'attribution':'Not Past chat'}]}],
            'flag':True,'number':1,'text':'true','empty':None,'values':['1',False],
            'odd':['scalar',9,None,{'items':[{'attribution':'Past chat'}]}]}
        export[0]['mapping']['assistant-node']['message']['metadata']=md
        path.write_text(json.dumps(export));app.import_paths([path])
        with app.db() as conn:
            def query(path,value):
                return app.api_messages(conn,{'meta_path':[path],'meta_value':[json.dumps(value)]})
            p=query('content_references[*].items[*].attribution','Past chat')
            self.assertEqual(p['total'],1)
            self.assertEqual(p['items'][0]['match_path'],'message.metadata.content_references[0].items[0].attribution')
            self.assertEqual(p['items'][0]['matching_field_count'],1)
            self.assertEqual(query('content_references[0].items[0].attribution','past chat')['total'],0)
            self.assertEqual(query('flag',True)['total'],1)
            self.assertEqual(query('flag',1)['total'],0)
            self.assertEqual(query('text',True)['total'],0)
            self.assertEqual(query('number',1.0)['total'],1)
            self.assertEqual(query('empty',None)['total'],1)
            self.assertEqual(query('missing',None)['total'],0)
            self.assertEqual(query('values[*]',1)['total'],0)
            self.assertEqual(query('values[*]','1')['total'],1)
            self.assertEqual(query('values[*]',False)['total'],1)
            self.assertEqual(query('odd[*].items[*].attribution','Past chat')['total'],1)
            self.assertEqual(query('flag[*]',True)['total'],0)
            with self.assertRaises(ValueError): query("flag') OR 1=1 --",True)
            with self.assertRaises(ValueError): query('flag',{})
            self.assertEqual(app.api_messages(conn,{'meta_path':['flag'],'meta_value':['true'],'role':['user']})['total'],0)

    def test_matching_passage_locates_late_instruction(self):
        obj={'user_profile':'Boilerplate '*100,'user_instructions':'Setup '*100+'You will be called DemoHelper. End.'}
        hit=app.matching_passage(obj,'You will be called DemoHelper','message.content')
        self.assertEqual(hit[0],'message.content.user_instructions')
        self.assertIn('You will be called DemoHelper',hit[1])
        self.assertLess(len(hit[1]),370)

    def test_context_search_and_existing_index_repair(self):
        path = self.write_export()
        export = json.loads(path.read_text())
        export[0]['mapping']['user-node']['message']['content'] = {
            'content_type': 'user_editable_context',
            'user_profile': 'Profile needle',
            'user_instructions': 'You will be called DemoHelper. Be precise.',
        }
        path.write_text(json.dumps(export))
        app.import_paths([path])
        with app.db() as conn:
            page = app.api_messages(conn, {'q':['You will be called DemoHelper']})
            self.assertEqual(page['total'], 1)
            pk = page['items'][0]['pk']
            original = conn.execute('SELECT raw_json,canonical_content_hash,visible_text_hash FROM messages WHERE pk=?',(pk,)).fetchone()
            conn.execute("UPDATE messages SET content_text='' WHERE pk=?",(pk,))
            conn.execute("UPDATE messages_fts SET content_text='' WHERE rowid=?",(pk,))
            self.assertEqual(app.api_messages(conn, {'q':['You will be called DemoHelper']})['total'],0)
            self.assertEqual(app.repair_context_search(conn),1)
            self.assertEqual(app.repair_context_search(conn),0)
            self.assertEqual(app.api_messages(conn, {'q':['You will be called DemoHelper']})['total'],1)
            self.assertEqual(app.api_messages(conn, {'q':['Profile needle']})['total'],1)
            after = conn.execute('SELECT raw_json,canonical_content_hash,visible_text_hash FROM messages WHERE pk=?',(pk,)).fetchone()
            self.assertEqual(tuple(original),tuple(after))

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(setattr, app, 'DB_PATH', app.DB_PATH)
        app.DB_PATH = Path(self.tmp.name) / "traceglass-test.sqlite3"
        app.init_db()

    def write_export(self):
        root = "root-node"
        user = "user-node"
        assistant = "assistant-node"
        conversation = {
            "id": "conversation-1",
            "title": "Fixture conversation",
            "create_time": 1000,
            "current_node": assistant,
            "mapping": {
                root: {"id": root, "message": None, "parent": None, "children": [user]},
                user: {
                    "id": user,
                    "parent": root,
                    "children": [assistant],
                    "message": {
                        "id": user,
                        "author": {"role": "user", "name": None},
                        "create_time": 1740001010,
                        "content": {"content_type": "text", "parts": ["needle phrase"]},
                        "metadata": {"request_id": "req-1", "parent_id": "different-parent"},
                        "status": "finished_successfully",
                        "recipient": "all",
                    },
                },
                assistant: {
                    "id": assistant,
                    "parent": user,
                    "children": [],
                    "message": {
                        "id": assistant,
                        "author": {"role": "assistant", "name": None},
                        "create_time": 1740001005,
                        "content": {"content_type": "text", "parts": ["response"]},
                        "metadata": {
                            "request_id": "req-1",
                            "model_slug": "fixture-model",
                            "finish_details": {"type": "stop", "stop_tokens": [200002]},
                        },
                        "status": "finished_successfully",
                        "recipient": "all",
                    },
                },
            },
        }
        path = Path(self.tmp.name) / "conversations.json"
        path.write_text(json.dumps([conversation]), encoding="utf-8")
        return path

    def test_raw_export_import_search_anomalies_and_lineage(self):
        path = self.write_export()
        result = app.import_paths([path])
        self.assertEqual(result["files"], 1)
        self.assertEqual(result["rows"], 3)

        with app.db() as conn:
            page = app.api_messages(conn, {"q": ["needle phrase"], "page_size": ["20"]})
            self.assertEqual(page["total"], 1)
            self.assertEqual(page["items"][0]["node_id"], "user-node")

            rules = {row[0] for row in conn.execute("SELECT DISTINCT rule FROM anomalies")}
            self.assertIn("metadata_parent_mismatch", rules)
            self.assertIn("timestamp_before_parent", rules)
            self.assertIn("stop_token_marker", rules)

            lineage = app.api_lineage(conn, {
                "source_id": ["1"], "conversation_id": ["conversation-1"],
                "focus": ["user-node"], "depth": ["4"],
            })
            self.assertEqual(len(lineage["nodes"]), 3)
            self.assertEqual(len(lineage["edges"]), 2)

            detail = app.api_message(conn, page["items"][0]["pk"])
            self.assertEqual(detail["metadata"]["request_id"], "req-1")
            self.assertTrue(detail["source_sha256"])

    def test_parent_checks_literal_walks_and_failures(self):
        app.import_paths([self.write_export()])
        with app.db() as conn:
            sid = conn.execute('SELECT source_id FROM messages LIMIT 1').fetchone()[0]

            def check(node='assistant-node', limit=200):
                return app.parent_trace_checks(conn, sid, 'conversation-1', node, limit)

            result = check()
            self.assertEqual(result['outer']['stop']['reason'], 'null_parent')
            self.assertEqual(len(result['outer']['steps']), 3)
            self.assertEqual(result['outer']['issues'], [])
            self.assertEqual(result['metadata']['stop']['reason'], 'missing_field')
            self.assertEqual(result['comparison'], 'metadata_parent_missing')
            self.assertEqual(check('user-node')['comparison'], 'different')
            self.assertEqual(check('user-node')['metadata']['stop']['reason'], 'target_not_in_source')
            self.assertEqual(check(limit=1)['outer']['stop']['reason'], 'limit')

            conn.execute("UPDATE messages SET metadata_json=? WHERE node_id='assistant-node'",
                         (json.dumps({'parent_id': None}),))
            self.assertEqual(check()['metadata']['stop']['reason'], 'null_parent')
            conn.execute("UPDATE messages SET children_json='[]' WHERE node_id='user-node'")
            self.assertEqual(check()['outer']['issues'][0]['reason'], 'missing_child_backlink')
            conn.execute("UPDATE messages SET parent_id='assistant-node',raw_json=json_set(raw_json,'$.parent','assistant-node') WHERE node_id='root-node'")
            self.assertEqual(check()['outer']['stop']['reason'], 'cycle')
            conn.execute("UPDATE messages SET parent_id='absent-node',raw_json=json_set(raw_json,'$.parent','absent-node') WHERE node_id='root-node'")
            self.assertEqual(check()['outer']['stop']['reason'], 'target_not_in_source')
            conn.execute("UPDATE messages SET parent_id=NULL,raw_json=json_remove(raw_json,'$.parent') WHERE node_id='root-node'")
            self.assertEqual(check()['outer']['stop']['reason'], 'missing_field')

    def test_parent_version_comparison_preserves_copies_and_missing_fields(self):
        path = self.write_export()
        app.import_paths([path])
        version = json.loads(path.read_text())
        mapping = version[0]['mapping']
        mapping['assistant-node']['message']['metadata']['parent_id'] = 'user-node'
        mapping['assistant-node']['parent'] = 'root-node'
        del mapping['user-node']
        changed = Path(self.tmp.name) / 'split.json'
        changed.write_text(json.dumps(version))
        app.import_paths([changed])
        with app.db() as conn:
            versions = app.parent_versions(conn, 'conversation-1', 'assistant-node')
            self.assertEqual(len(versions['items']), 2)
            newest, oldest = versions['items']
            self.assertEqual(newest['outer']['value'], 'root-node')
            self.assertEqual(newest['metadata_parent']['value'], 'user-node')
            self.assertEqual(newest['metadata_parent']['target_scope'], 'absent_in_dataset')
            self.assertEqual(oldest['outer']['targets'][0]['role'], 'user')
            self.assertEqual(oldest['outer']['target_scope'], 'same_source')
            self.assertEqual(oldest['metadata_parent']['state'], 'missing')
            root_versions = app.parent_versions(conn, 'conversation-1', 'root-node')
            self.assertEqual(root_versions['items'][0]['outer']['state'], 'null')
            detail = app.api_message(conn, newest['pk'])
            self.assertEqual(len(detail['parent_versions']['items']), 2)

    def test_parent_key_candidates_do_not_call_correct_keys_typos(self):
        raw = {'parent': 'a', 'message': {'metadata': {'parent_id': 'b', 'request_id': 'r'}}}
        self.assertEqual(app.parent_key_candidates(raw), [])
        raw['message']['metadata']['parnet_id'] = 'c'
        candidates = app.parent_key_candidates(raw)
        self.assertEqual([c['path'] for c in candidates], ['node.message.metadata.parnet_id'])
        raw['parentID'] = None
        self.assertIn('node.parentID', [c['path'] for c in app.parent_key_candidates(raw)])

    def test_static_path_cannot_escape_root(self):
        static = app.STATIC.resolve()
        escaped = (app.STATIC / "../app.py").resolve()
        self.assertNotIn(static, escaped.parents)

    def test_cross_file_lineage_dataset_scope_and_duplicate_source(self):
        parent_id = "parent-across-file"
        child_id = "child-across-file"
        conv = {"conversation_id": "split-conversation", "title": "Split lineage"}
        parent = {
            "id": parent_id, "parent": None, "children": [child_id],
            "message": {
                "id": parent_id, "author": {"role": "user"}, "create_time": 1740002000,
                "content": {"content_type": "text", "parts": ["parent record"]},
                "metadata": {}, "status": "finished_successfully", "recipient": "all",
            },
        }
        child = {
            "id": child_id, "parent": parent_id, "children": [],
            "message": {
                "id": child_id, "author": {"role": "assistant"}, "create_time": 1740002001,
                "content": {"content_type": "text", "parts": ["child record"]},
                "metadata": {}, "status": "finished_successfully", "recipient": "all",
            },
        }

        def chronological(node, sequence):
            message = node["message"]
            mapping_node = {k: v for k, v in node.items() if k != "message"}
            return {
                "reader_schema": "fixture", "conversation": conv,
                "mapping_node": mapping_node, "message": message,
                "chronological_sequence": sequence,
            }

        first = Path(self.tmp.name) / "first.ndjson"
        second = Path(self.tmp.name) / "second.ndjson"
        first.write_text(json.dumps(chronological(parent, 1)) + "\n", encoding="utf-8")
        second.write_text(json.dumps(chronological(child, 2)) + "\n", encoding="utf-8")
        result = app.import_paths([first, second])
        self.assertEqual(result["rows"], 2)

        with app.db() as conn:
            child_row = conn.execute(
                "SELECT source_id FROM messages WHERE node_id=?", (child_id,)
            ).fetchone()
            false_missing = conn.execute(
                "SELECT COUNT(*) FROM anomalies WHERE node_id=? AND rule='missing_parent_node'",
                (child_id,),
            ).fetchone()[0]
            self.assertEqual(false_missing, 0)
            literal = app.parent_trace_checks(conn, child_row['source_id'], conv['conversation_id'], child_id)
            self.assertEqual(literal['outer']['stop']['reason'], 'target_not_in_source')
            self.assertEqual(literal['outer']['stop']['node_id'], parent_id)
            versions = app.parent_versions(conn, conv['conversation_id'], child_id)
            self.assertEqual(versions['items'][0]['outer']['target_scope'], 'other_file_same_dataset')
            lineage = app.api_lineage(conn, {
                "source_id": [str(child_row["source_id"])],
                "conversation_id": [conv["conversation_id"]],
                "focus": [child_id], "depth": ["4"],
            })
            self.assertEqual({n["id"] for n in lineage["nodes"]}, {parent_id, child_id})

        duplicate = Path(self.tmp.name) / "duplicate.ndjson"
        shutil.copyfile(first, duplicate)
        second_result = app.import_paths([duplicate])
        with app.db() as conn:
            latest = app.api_stats(conn, {"dataset_id": [str(second_result["dataset_id"])]})
            original = app.api_stats(conn, {"dataset_id": [str(result["dataset_id"])]})
            self.assertEqual(latest["totals"]["messages"], 1)
            self.assertEqual(original["totals"]["messages"], 2)
            self.assertEqual(latest["sources"][0]["duplicate_copies"], 1)

    def test_cross_dataset_reconciliation_separates_text_structure_and_omission(self):
        def write_version(name, content):
            node_id = "stable-node"
            conversation = {
                "id": "stable-conversation", "title": "Version fixture",
                "mapping": {
                    node_id: {
                        "id": node_id, "parent": None, "children": [],
                        "message": {
                            "id": node_id, "author": {"role": "assistant"},
                            "create_time": 1740003000, "content": content,
                            "metadata": {}, "status": "finished_successfully",
                            "recipient": "all",
                        },
                    }
                },
            }
            path = Path(self.tmp.name) / f"{name}.json"
            path.write_text(json.dumps([conversation]), encoding="utf-8")
            return app.import_paths([path])["dataset_id"]

        base = write_version("base", {
            "content_type": "multimodal_text",
            "parts": [{"content_type": "audio_transcription", "text": "hello"},
                      {"content_type": "audio_asset_pointer", "size_bytes": 10}],
        })
        structural = write_version("structural", {
            "parts": [{"text": "hello", "content_type": "audio_transcription"},
                      {"size_bytes": 11, "content_type": "audio_asset_pointer"}],
            "content_type": "multimodal_text",
        })
        omitted = write_version("omitted", {
            "content_type": "reasoning_recap", "content_omitted": True,
        })
        changed = write_version("changed", {
            "content_type": "text", "parts": ["goodbye"],
        })

        with app.db() as conn:
            base_stats = app.api_stats(conn, {"dataset_id": [str(base)]})["totals"]
            structural_stats = app.api_stats(conn, {"dataset_id": [str(structural)]})["totals"]
            omitted_stats = app.api_stats(conn, {"dataset_id": [str(omitted)]})["totals"]
            changed_stats = app.api_stats(conn, {"dataset_id": [str(changed)]})["totals"]
            self.assertEqual(base_stats["conflicting_events_elsewhere"], 1)
            self.assertEqual(structural_stats["variant_events_elsewhere"], 1)
            self.assertEqual(omitted_stats["omitted_events_elsewhere"], 1)
            self.assertEqual(changed_stats["conflicting_events_elsewhere"], 1)

            changed_rules = {r["rule"] for r in app.api_stats(
                conn, {"dataset_id": [str(changed)]}
            )["rules"]}
            self.assertIn("cross_dataset_text_conflict", changed_rules)
            self.assertIn("cross_dataset_content_omission", changed_rules)

    def test_lineage_preserves_hidden_cross_export_parent_and_id_search(self):
        user_id = "9e09ad71-8483-445d-af16-e1718ff55bc4"
        system_id = "9c228349-6725-4338-af40-6f6598d335e2"
        assistant_id = "dd357614-072a-413f-a58e-66b207f896f1"

        def message(node_id, role, parent, children, text, metadata=None):
            return {
                "id": node_id, "parent": parent, "children": children,
                "message": {
                    "id": node_id, "author": {"role": role}, "create_time": 1740004000,
                    "content": {"content_type": "text", "parts": [text]},
                    "metadata": metadata or {}, "status": "finished_successfully",
                    "recipient": "all",
                },
            }

        compact = {
            "id": "versioned-conversation", "title": "Versioned lineage",
            "mapping": {
                user_id: message(user_id, "user", None, [assistant_id], "question"),
                assistant_id: message(
                    assistant_id, "assistant", user_id, [], "answer",
                    {"parent_id": system_id, "request_id": "req_shared"},
                ),
            },
        }
        complete = {
            "id": "versioned-conversation", "title": "Versioned lineage",
            "mapping": {
                user_id: message(user_id, "user", None, [system_id], "question"),
                system_id: message(
                    system_id, "system", user_id, [assistant_id], "",
                    {"parent_id": user_id, "is_visually_hidden_from_conversation": True,
                     "is_contextual_answers_system_message": True},
                ),
                assistant_id: message(
                    assistant_id, "assistant", system_id, [], "answer",
                    {"parent_id": system_id, "request_id": "req_shared"},
                ),
            },
        }
        compact_path = Path(self.tmp.name) / "compact.json"
        complete_path = Path(self.tmp.name) / "complete.json"
        compact_path.write_text(json.dumps([compact]), encoding="utf-8")
        complete_path.write_text(json.dumps([complete]), encoding="utf-8")
        compact_dataset = app.import_paths([compact_path])["dataset_id"]
        complete_dataset = app.import_paths([complete_path])["dataset_id"]
        self.assertNotEqual(compact_dataset, complete_dataset)

        with app.db() as conn:
            compact_source = conn.execute(
                "SELECT id FROM sources WHERE dataset_id=?", (compact_dataset,)
            ).fetchone()[0]
            lineage = app.api_lineage(conn, {
                "source_id": [str(compact_source)],
                "conversation_id": ["versioned-conversation"],
                "focus": [assistant_id], "depth": ["5"],
            })
            nodes = {node["id"]: node for node in lineage["nodes"]}
            self.assertIn(system_id, nodes)
            self.assertTrue(nodes[system_id]["hidden"])
            self.assertTrue(nodes[system_id]["supplemental"])
            self.assertEqual(nodes[assistant_id]["graph_parent"], user_id)
            self.assertEqual(nodes[assistant_id]["metadata_parent"], system_id)
            self.assertTrue(nodes[assistant_id]["metadata_parent_corroborated"])
            edge = next(e for e in lineage["edges"] if e["source"] == system_id and e["target"] == assistant_id)
            self.assertIn("metadata_parent", edge["kinds"])
            self.assertIn("other_export_graph", edge["kinds"])

            page = app.api_messages(conn, {
                "q": [system_id], "dataset_id": [str(complete_dataset)], "page_size": ["20"]
            })
            self.assertEqual(page["total"], 1)
            self.assertEqual(page["items"][0]["node_id"], system_id)

    def test_hidden_metadata_markers_exclude_message_text_and_raw_view_reads_skipped_json(self):
        plain_id = "plain-hidden-word"
        marked_id = "metadata-hidden-field"
        conversation = {
            "id": "hidden-metadata-conversation", "title": "Hidden metadata fixture",
            "mapping": {
                plain_id: {
                    "id": plain_id, "parent": None, "children": [marked_id],
                    "message": {
                        "id": plain_id, "author": {"role": "user"},
                        "create_time": 1740005000,
                        "content": {"content_type": "text", "parts": ["the word hidden is only message text"]},
                        "metadata": {}, "status": "finished_successfully", "recipient": "all",
                    },
                },
                marked_id: {
                    "id": marked_id, "parent": plain_id, "children": [],
                    "message": {
                        "id": marked_id, "author": {
                            "role": "system", "metadata": {"hidden_author_note": "yes"},
                        },
                        "create_time": 1740005001,
                        "content": {"content_type": "text", "parts": [""]},
                        "metadata": {
                            "is_visually_hidden_from_conversation": True,
                            "routing": {"note": "hidden policy selected"},
                        },
                        "status": "finished_successfully", "recipient": "all",
                    },
                },
            },
        }
        conversations = Path(self.tmp.name) / "conversations.json"
        conversations.write_text(json.dumps([conversation]), encoding="utf-8")
        generic = Path(self.tmp.name) / "codex.json"
        generic.write_text(json.dumps({
            "metadata": {"some_hidden_setting": False},
            "unrelated": [1, 2, 3],
        }), encoding="utf-8")
        result = app.import_paths([conversations, generic])

        with app.db() as conn:
            rules = {
                (row["node_id"], row["rule"])
                for row in conn.execute("SELECT node_id,rule FROM anomalies")
            }
            self.assertNotIn((plain_id, "hidden_metadata_mention"), rules)
            self.assertIn((marked_id, "hidden_metadata_mention"), rules)
            self.assertIn((marked_id, "visually_hidden_from_conversation"), rules)

            metadata_page = app.api_messages(conn, {
                "q": ["meta:hidden"], "dataset_id": [str(result["dataset_id"])],
                "page_size": ["20"],
            })
            self.assertEqual(metadata_page["total"], 1)
            self.assertTrue(metadata_page["items"][0]["visually_hidden"])
            detail = app.api_message(conn, metadata_page["items"][0]["pk"])
            self.assertEqual(len(detail["hidden_metadata_hits"]), 3)
            self.assertIn(
                "message.author.metadata.hidden_author_note",
                {hit["path"] for hit in detail["hidden_metadata_hits"]},
            )

            generic_source = conn.execute(
                "SELECT id,status FROM sources WHERE path=?", (str(generic.resolve()),)
            ).fetchone()
            self.assertEqual(generic_source["status"], "skipped")
            raw = app.api_source_raw(conn, generic_source["id"], {
                "q": ["hidden"], "length": ["4096"], "match": ["0"],
            })
            self.assertEqual(raw["match_count"], 1)
            self.assertIn("some_hidden_setting", raw["content"])
            self.assertIn("unrelated", raw["content"])


if __name__ == "__main__":
    unittest.main()
