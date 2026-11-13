"""`Htmx.trigger` sends the values htmx 2.0.4 sends from the browser.

Each test fires a trigger on a page written by hand and reads the arguments it hands to the event
handler.  The rules come from `getInputValues`, `shouldInclude` and `querySelectorAllExt` in htmx.

"""

from typing import Any
from unittest.mock import patch

from django.test import Client, SimpleTestCase
from lxml import html

from djhtmx.introspection import parse_request_data
from djhtmx.testing import Htmx

POST = 'hx-post="/_htmx/Editor/editor/save_field"'


class TriggerValues(SimpleTestCase):
    def test_closest_sends_only_the_row_of_the_trigger(self):
        """The case of the bug report: two rows, each with its own include and key."""
        self.assertSent(
            f"""
            <div>
              <div data-attribute-row>
                <input name="field_name" value="Serial" {POST}
                       hx-include="closest [data-attribute-row]" hx-vals='{{"key": "a"}}'>
                <input name="field_shorthand" value="SN">
                <select name="field_type" disabled><option value="text">Text</option></select>
              </div>
              <div data-attribute-row>
                <input id="trigger" name="field_name" value="Model" {POST}
                       hx-include="closest [data-attribute-row]" hx-vals='{{"key": "b"}}'>
                <input name="field_shorthand" value="MD">
                <select name="field_type" disabled><option value="text">Text</option></select>
              </div>
            </div>
            """,
            {"field_name": "Model", "field_shorthand": "MD", "key": "b"},
        )

    def test_find_next_and_previous_start_from_the_trigger(self):
        self.assertSent(
            f"""
            <div>
              <input name="before" value="far">
              <input name="before" value="near">
              <div id="trigger" {POST} hx-include="find input, next input, previous input">
                <input name="inside" value="1"><input name="inside" value="2">
              </div>
              <input name="after" value="near">
              <input name="after" value="far">
            </div>
            """,
            {"before": "near", "inside": "1", "after": "near"},
        )

    def test_a_plain_css_include_expands_a_container(self):
        self.assertSent(
            f"""
            <div>
              <button id="trigger" {POST} hx-include="#box"></button>
              <div id="box"><input name="a" value="1"><p><input name="b" value="2"></p></div>
            </div>
            """,
            {"a": "1", "b": "2"},
        )

    def test_this_is_the_element_that_declares_the_include(self):
        self.assertSent(
            f"""
            <div hx-include="this">
              <input name="a" value="1">
              <button id="trigger" {POST}></button>
            </div>
            """,
            {"a": "1"},
        )

    def test_the_trigger_sends_its_own_value_once(self):
        self.assertSent(
            f"""
            <div id="box">
              <input id="trigger" name="a" value="1" {POST} hx-include="#box [name]">
              <input name="b" value="2" hx-include="#elsewhere">
            </div>
            """,
            {"a": "1", "b": "2"},
            raw={"a": ["1"], "b": ["2"]},
        )

    def test_the_trigger_sends_its_value_without_an_include(self):
        self.assertSent(f'<input id="trigger" name="a" value="1" {POST}>', {"a": "1"})

    def test_the_include_is_inherited(self):
        self.assertSent(
            f"""
            <div hx-include="#box">
              <button id="trigger" {POST}></button>
              <div id="box"><input name="a" value="1"></div>
            </div>
            """,
            {"a": "1"},
        )

    def test_hx_vals_merge_up_the_tree_and_the_nearest_wins(self):
        self.assertSent(
            f"""
            <div hx-vals='{{"a": "far", "b": "far"}}'>
              <div hx-vals='"a": "near"'>
                <button id="trigger" {POST} hx-vals='{{"c": "own"}}'></button>
              </div>
            </div>
            """,
            {"a": "near", "b": "far", "c": "own"},
        )

    def test_hx_vals_win_over_field_values(self):
        self.assertSent(
            f"""<input id="trigger" name="a" value="field" {POST} hx-vals='{{"a": "vals"}}'>""",
            {"a": "vals"},
        )

    def test_the_closest_form_is_sent_and_wins(self):
        self.assertSent(
            f"""
            <div>
              <form>
                <input name="a" value="form"><input name="b" value="form">
                <button id="trigger" {POST} hx-include="#outside"></button>
              </form>
              <div id="outside"><input name="a" value="outside"><input name="c" value="3"></div>
            </div>
            """,
            {"a": "form", "b": "form", "c": "3"},
        )

    def test_fields_htmx_skips(self):
        self.assertSent(
            f"""
            <div id="box">
              <button id="trigger" {POST} hx-include="#box"></button>
              <input name="disabled" value="x" disabled>
              <fieldset disabled><input name="in_disabled_fieldset" value="x"></fieldset>
              <input type="button" name="button" value="x">
              <input type="submit" name="submit" value="x">
              <input type="checkbox" name="unchecked" value="x">
              <input type="radio" name="unchecked_radio" value="x">
              <input value="no name">
              <input name="kept" value="1">
            </div>
            """,
            {"kept": "1"},
        )

    def test_field_values(self):
        self.assertSent(
            f"""
            <div id="box">
              <button id="trigger" {POST} hx-include="#box"></button>
              <textarea name="text">Some text</textarea>
              <select name="first"><option value="x">X</option><option value="y">Y</option></select>
              <select name="picked"><option value="x">X</option><option selected>  Y  Z </option></select>
              <select name="tags[]" multiple>
                <option value="a" selected>A</option><option value="b">B</option>
                <option value="c" selected>C</option>
              </select>
              <input type="checkbox" name="checked" checked>
              <input name="empty">
            </div>
            """,
            {
                "text": "Some text",
                "first": "x",
                "picked": "Y Z",
                "tags": ["a", "c"],
                "checked": "on",
                "empty": "",
            },
        )

    def test_typing_into_a_textarea_changes_what_it_sends(self):
        htmx = self.htmx_on(
            f"""<div id="box"><textarea name="text">Old</textarea>
            <button id="trigger" {POST} hx-include="#box"></button></div>"""
        )
        htmx.type_into("textarea", "New", clear=True)
        self.assertEqual(self.trigger(htmx)[2], {"text": "New"})

    def assertSent(self, markup: str, expected: dict[str, Any], raw=None):
        htmx = self.htmx_on(markup)
        with patch("djhtmx.testing.parse_request_data", wraps=parse_request_data) as parse:
            component_id, handler, sent = self.trigger(htmx)
        self.assertEqual((component_id, handler), ("editor", "save_field"))
        self.assertEqual(sent, expected)
        if raw is not None:
            self.assertEqual({k: list(v) for k, v in parse.call_args.args[0].items()}, raw)

    def htmx_on(self, markup: str) -> Htmx:
        htmx = Htmx(Client())
        htmx.dom = html.fromstring(f"<html><body>{markup}</body></html>")
        return htmx

    def trigger(self, htmx: Htmx):
        with patch.object(htmx, "dispatch_event") as dispatch:
            htmx.trigger("#trigger")
        return dispatch.call_args.args
