from bot.handlers import download as download_handlers


async def test_channel_post_replacement_propagates_telemetry_job_id(monkeypatch):
    captured = {}

    async def fake_send_media(context, chat_id, path, result, caption, reply_markup=None,
                              **kwargs):
        captured.update(kwargs)
        return object()

    async def fake_try_delete(context, chat_id, message_id):
        return False

    monkeypatch.setattr(download_handlers, "_send_media", fake_send_media)
    monkeypatch.setattr(download_handlers, "_try_delete", fake_try_delete)

    outcome = await download_handlers._replace_channel_post(
        object(), 123, 456, None, object(), job_id="opaque123"
    )

    assert outcome == "sent"
    assert captured["job_id"] == "opaque123"
