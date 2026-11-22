import threading

from multiple_description_progress import emit, page_list, waiting


def test_heartbeat_only_runs_while_waiting_and_never_sends_request():
    heard = threading.Event()
    messages = []
    def receive(text):
        messages.append(text)
        heard.set()
    with waiting(receive, interval=.01):
        assert heard.wait(2)
    count = len(messages)
    assert count and 'Запрос уже отправлен' in messages[0]
    assert not any(t.name == 'multi-progress' and t.is_alive() for t in threading.enumerate())
    assert len(messages) == count


def test_bad_progress_receiver_cannot_break_processing():
    def bad(_):
        raise ValueError('sensitive')
    emit(bad, 'hello')
    with waiting(bad, interval=.01):
        pass
    assert page_list(list(range(1, 25))).endswith('и ещё 4')


def test_cancellation_from_progress_prevents_sending_another_paid_request():
    import pytest
    import multiple_description_parse as md
    from multiple_description_cancel import Cancelled
    from test_multiple_description_parse import ON, pages
    active = [True]
    calls = []
    def progress(message):
        if message.startswith('Разбор: запрос'):
            active[0] = False
    with pytest.raises(Cancelled):
        md.parse_pages(ON, pages(1), image_limit=1, filename_lot=None, portal_lot=None,
                       search_complete=True, send=lambda *a: calls.append(1),
                       current_job=lambda: active[0], progress=progress)
    assert not calls
