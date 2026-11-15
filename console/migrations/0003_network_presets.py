from django.db import migrations


def add_presets(apps, schema_editor):
    profile = apps.get_model("console", "NetworkProfile")
    for name, down, up, delay, jitter, loss in (
        ("No throttling", 0, 0, 0, 0, 0),
        ("Fibre / Fast Wi-Fi", 100000, 50000, 5, 0, 0),
        ("4G", 20000, 10000, 50, 5, 0),
        ("Fast 3G", 1600, 750, 150, 10, 0),
        ("Slow 3G", 400, 400, 400, 30, 1),
        ("2G / Edge", 250, 50, 800, 60, 2),
        ("Flaky mobile", 1000, 500, 300, 100, 10),
    ):
        profile.objects.using(schema_editor.connection.alias).get_or_create(
            name=name, defaults=dict(download_kbps=down, upload_kbps=up, latency_ms=delay,
                                     jitter_ms=jitter, loss_pct=loss, preset=True))


class Migration(migrations.Migration):
    dependencies = [("console", "0002_monitoring_workspace")]
    operations = [migrations.RunPython(add_presets, migrations.RunPython.noop)]
