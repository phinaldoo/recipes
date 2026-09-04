from __future__ import annotations

import secrets
from pathlib import Path

import typer
from sqlalchemy import func, select

from app.auth.security import hash_password
from app.backups import export_backup, preflight_backup
from app.database import SessionLocal
from app.models import AuditLog, User, UserSession

cli = typer.Typer(
    name="rezepte",
    help="Administration der privaten Rezeptverwaltung",
    no_args_is_help=True,
)
users = typer.Typer(help="Benutzerkonten verwalten", no_args_is_help=True)
backups = typer.Typer(help="Backups prüfen und erstellen", no_args_is_help=True)
cli.add_typer(users, name="users")
cli.add_typer(backups, name="backups")


def normalize_email(email: str) -> str:
    value = email.strip().casefold()
    if "@" not in value or value.startswith("@") or value.endswith("@") or len(value) > 320:
        raise typer.BadParameter("Bitte eine gültige E-Mail-Adresse angeben")
    return value


def read_new_password(generated: bool = False) -> str:
    if generated:
        password = secrets.token_urlsafe(18)
        typer.echo(f"Einmaliges Startpasswort: {password}")
        typer.echo("Bitte übermittle es ausschließlich über einen sicheren Kanal.")
        return password
    first = typer.prompt("Passwort (mindestens 12 Zeichen)", hide_input=True)
    second = typer.prompt("Passwort wiederholen", hide_input=True)
    if first != second:
        raise typer.BadParameter("Die Passwörter stimmen nicht überein")
    return str(first)


@users.command("create")
def create_user(
    email: str = typer.Option(..., "--email"),
    display_name: str | None = typer.Option(None, "--display-name"),
    role: str = typer.Option("member", "--role"),
    generate_password: bool = typer.Option(False, "--generate-password"),
) -> None:
    """Legt ein neues Konto an; es gibt bewusst keine öffentliche Registrierung."""
    if role not in {"member", "admin"}:
        raise typer.BadParameter("Die Rolle muss member oder admin sein")
    normalized = normalize_email(email)
    password = read_new_password(generate_password)
    with SessionLocal() as db:
        if db.scalar(select(User.id).where(User.email == normalized)):
            raise typer.BadParameter("Für diese E-Mail-Adresse existiert bereits ein Konto")
        user = User(
            email=normalized,
            display_name=display_name.strip() if display_name else None,
            password_hash=hash_password(password),
            role=role,
            is_active=True,
        )
        db.add(user)
        db.flush()
        db.add(
            AuditLog(
                actor_user_id=None,
                action="user.create.cli",
                target_type="user",
                target_id=str(user.id),
                details={"email": normalized, "role": role},
            )
        )
        db.commit()
    typer.echo(f"Benutzer {normalized} wurde als {role} angelegt.")


@users.command("list")
def list_users() -> None:
    with SessionLocal() as db:
        records = list(db.scalars(select(User).order_by(func.lower(User.email))))
    if not records:
        typer.echo("Noch keine Benutzer vorhanden.")
        return
    for user in records:
        state = "aktiv" if user.is_active else "deaktiviert"
        typer.echo(f"{user.email}\t{user.display_name or '–'}\t{user.role}\t{state}")


@users.command("reset-password")
def reset_password(
    email: str = typer.Option(..., "--email"),
    generate_password: bool = typer.Option(False, "--generate-password"),
) -> None:
    password = read_new_password(generate_password)
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.email == normalize_email(email)))
        if user is None:
            raise typer.BadParameter("Das Benutzerkonto wurde nicht gefunden")
        user.password_hash = hash_password(password)
        db.query(UserSession).filter(UserSession.user_id == user.id).delete(
            synchronize_session=False
        )
        db.add(
            AuditLog(
                actor_user_id=None,
                action="user.password_reset.cli",
                target_type="user",
                target_id=str(user.id),
            )
        )
        db.commit()
    typer.echo("Passwort geändert; alle bestehenden Sitzungen wurden beendet.")


@users.command("set-role")
def set_role(
    email: str = typer.Option(..., "--email"), role: str = typer.Option(..., "--role")
) -> None:
    if role not in {"member", "admin"}:
        raise typer.BadParameter("Die Rolle muss member oder admin sein")
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.email == normalize_email(email)))
        if user is None:
            raise typer.BadParameter("Das Benutzerkonto wurde nicht gefunden")
        if user.role == "admin" and role != "admin":
            admin_count = db.scalar(
                select(func.count()).select_from(User).where(User.role == "admin", User.is_active)
            )
            if admin_count == 1:
                raise typer.BadParameter(
                    "Der letzte aktive Administrator kann nicht herabgestuft werden"
                )
        old_role = user.role
        user.role = role
        db.query(UserSession).filter(UserSession.user_id == user.id).delete(
            synchronize_session=False
        )
        db.add(
            AuditLog(
                actor_user_id=None,
                action="user.role_change.cli",
                target_type="user",
                target_id=str(user.id),
                details={"from": old_role, "to": role},
            )
        )
        db.commit()
    typer.echo(f"Rolle auf {role} gesetzt; bestehende Sitzungen wurden beendet.")


@users.command("deactivate")
def deactivate(email: str = typer.Option(..., "--email")) -> None:
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.email == normalize_email(email)))
        if user is None:
            raise typer.BadParameter("Das Benutzerkonto wurde nicht gefunden")
        if user.role == "admin" and user.is_active:
            admin_count = db.scalar(
                select(func.count()).select_from(User).where(User.role == "admin", User.is_active)
            )
            if admin_count == 1:
                raise typer.BadParameter(
                    "Der letzte aktive Administrator kann nicht deaktiviert werden"
                )
        user.is_active = False
        db.query(UserSession).filter(UserSession.user_id == user.id).delete(
            synchronize_session=False
        )
        db.add(
            AuditLog(
                actor_user_id=None,
                action="user.deactivate.cli",
                target_type="user",
                target_id=str(user.id),
            )
        )
        db.commit()
    typer.echo("Benutzer deaktiviert; Kommentare und Autorensnapshots bleiben erhalten.")


@backups.command("create")
def create_backup() -> None:
    """Erstellt synchron ein verifiziertes Vollbackup, geeignet für Cron/systemd."""
    with SessionLocal() as db:
        path, manifest, digest = export_backup(db)
    typer.echo(f"Backup erstellt: {path}")
    typer.echo(f"SHA-256: {digest}")
    typer.echo(
        f"Rezepte: {manifest.counts.get('recipes', 0)} · Dateien: {manifest.media_file_count}"
    )


@backups.command("verify")
def verify_backup(path: Path = typer.Argument(..., exists=True, dir_okay=False)) -> None:
    result = preflight_backup(path)
    typer.echo("Backup ist gültig.")
    typer.echo(
        f"Version {result.application_version} · {result.media_file_count} Dateien · "
        f"{result.media_total_bytes} Bytes"
    )


if __name__ == "__main__":
    cli()
