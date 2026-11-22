from looma.prompts import readable


def test_plain_prompt_is_unchanged():
    assert readable("add a docstring to hello") == "add a docstring to hello"


def test_pulls_the_users_words_out_of_app_markup():
    wrapped = """<wake reason="mention">
  <project id="p"><thread ts="t">
    <message trigger="true" from="human" trust="principal" id="m">make the button &amp; label bigger</message>
  </thread></project>
  <system-note>harness text</system-note>
</wake>"""
    assert readable(wrapped) == "make the button & label bigger"


def test_prefers_cited_user_text_then_falls_back_to_the_note():
    relay = """<relay from="coordinator">
  <cited id="c1" author="user" name="V">ok fix the readme</cited>
  <note>Vardaan wants a README fix.</note>
</relay>"""
    assert readable(relay) == "ok fix the readme"
    assert readable('<relay from="coordinator"><note>Make one small edit.</note></relay>') == "Make one small edit."
