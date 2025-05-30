from typing import TYPE_CHECKING, Any, TypeAlias

import structlog

from polar.exceptions import PolarError
from polar.integrations.github.client import GitHub, TokenAuthStrategy
from polar.kit.extensions.sqlalchemy import sql
from polar.locker import Locker
from polar.models import OAuthAccount, User
from polar.models.user import OAuthPlatform
from polar.postgres import AsyncSession
from polar.user.oauth_service import oauth_account_service
from polar.user.schemas.user import UserSignupAttribution
from polar.user.service.user import UserService
from polar.worker import enqueue_job

if TYPE_CHECKING:
    from githubkit.versions.latest.models import PrivateUser, PublicUser

from .. import client as github
from ..schemas import OAuthAccessToken

log = structlog.get_logger()


GithubUser: TypeAlias = "PrivateUser | PublicUser"
GithubEmail: TypeAlias = tuple[str, bool]


class GithubUserServiceError(PolarError): ...


class NoPrimaryEmailError(GithubUserServiceError):
    def __init__(self) -> None:
        super().__init__("GitHub user without primary email set")


class CannotLinkUnverifiedEmailError(GithubUserServiceError):
    def __init__(self, email: str) -> None:
        message = (
            f"An account already exists on Polar under the email {email}. "
            "We cannot automatically link it to your GitHub account since "
            "this email address is not verified on GitHub. "
            "Either verify your email address on GitHub and try again "
            "or sign in with a magic link."
        )
        super().__init__(message, 403)


class AccountLinkedToAnotherUserError(GithubUserServiceError):
    def __init__(self) -> None:
        message = (
            "This GitHub account is already linked to another user on Polar. "
            "You may have already created another account "
            "with a different email address."
        )
        super().__init__(message, 403)


class GithubUserService(UserService):
    async def get_user_by_github_id(
        self, session: AsyncSession, id: int
    ) -> User | None:
        stmt = (
            sql.select(User)
            .join(OAuthAccount, User.id == OAuthAccount.user_id)
            .where(
                OAuthAccount.platform == OAuthPlatform.github,
                OAuthAccount.account_id == str(id),
            )
        )
        res = await session.execute(stmt)
        return res.scalars().first()

    async def get_user_by_github_username(
        self,
        session: AsyncSession,
        username: str,
    ) -> User | None:
        stmt = (
            sql.select(User)
            .join(OAuthAccount, User.id == OAuthAccount.user_id)
            .where(
                OAuthAccount.platform == OAuthPlatform.github,
                OAuthAccount.account_username == username,
            )
        )
        res = await session.execute(stmt)
        return res.scalars().first()

    def generate_profile_json(
        self,
        *,
        github_user: GithubUser,
    ) -> dict[str, Any]:
        return {
            "platform": "github",
            "name": github_user.name,
            "bio": github_user.bio,
            "company": github_user.company,
            "blog": github_user.blog,
            "location": github_user.location,
            "hireable": github_user.hireable,
            "twitter": github_user.twitter_username,
            "public_repos": github_user.public_repos,
            "public_gists": github_user.public_gists,
            "followers": github_user.followers,
            "following": github_user.following,
            "created_at": github_user.created_at.isoformat(),
            "updated_at": github_user.updated_at.isoformat(),
        }

    async def create(
        self,
        session: AsyncSession,
        *,
        github_user: GithubUser,
        github_email: GithubEmail,
        tokens: OAuthAccessToken,
        signup_attribution: UserSignupAttribution | None = None,
    ) -> User:
        email, email_verified = github_email
        new_user = User(
            email=email,
            email_verified=email_verified,
            avatar_url=github_user.avatar_url,
            signup_attribution=signup_attribution,
            oauth_accounts=[
                OAuthAccount(
                    platform=OAuthPlatform.github,
                    access_token=tokens.access_token,
                    expires_at=tokens.expires_at,
                    refresh_token=tokens.refresh_token,
                    refresh_token_expires_at=tokens.refresh_token_expires_at,
                    account_id=str(github_user.id),
                    account_email=email,
                    account_username=github_user.login,
                )
            ],
        )

        session.add(new_user)
        await session.flush()

        log.info("github.user.create", user_id=new_user.id, username=github_user.login)

        enqueue_job("user.on_after_signup", user_id=new_user.id)

        return new_user

    async def get_updated(
        self,
        session: AsyncSession,
        *,
        github_user: GithubUser,
        user: User,
        tokens: OAuthAccessToken,
        client: GitHub[TokenAuthStrategy],
    ) -> User:
        # Fetch primary email from github
        # Required to succeed for new users signups. For existing users we'll let it fail.
        github_email: GithubEmail | None = None
        try:
            github_email = await self.fetch_authenticated_user_primary_email(
                client=client
            )
        except NoPrimaryEmailError:
            pass

        user.avatar_url = github_user.avatar_url
        session.add(user)

        oauth_account = await oauth_account_service.get_by_platform_and_user_id(
            session, OAuthPlatform.github, user.id
        )
        if oauth_account is None:
            if github_email is None:
                raise NoPrimaryEmailError()

            email, _ = github_email

            oauth_account = OAuthAccount(
                platform=OAuthPlatform.github,
                account_id=str(github_user.id),
                account_email=email,
                account_username=github_user.login,
                user=user,
            )

        # update email if fetch was successful
        if github_email is not None:
            email, _ = github_email
            oauth_account.account_email = email

        oauth_account.access_token = tokens.access_token
        oauth_account.expires_at = tokens.expires_at
        oauth_account.refresh_token = tokens.refresh_token
        oauth_account.refresh_token_expires_at = tokens.refresh_token_expires_at
        oauth_account.account_username = github_user.login
        session.add(oauth_account)

        log.info("github.user.update", user_id=user.id)
        return user

    async def get_updated_or_create(
        self,
        session: AsyncSession,
        locker: Locker,
        *,
        tokens: OAuthAccessToken,
        signup_attribution: UserSignupAttribution | None = None,
    ) -> tuple[User, bool]:
        client = github.get_client(access_token=tokens.access_token)
        authenticated = await self.fetch_authenticated_user(client=client)

        user, created = await self._get_updated_or_create(
            session,
            tokens=tokens,
            client=client,
            authenticated=authenticated,
            signup_attribution=signup_attribution,
        )
        return (user, created)

    async def _get_updated_or_create(
        self,
        session: AsyncSession,
        *,
        tokens: OAuthAccessToken,
        client: GitHub[TokenAuthStrategy],
        authenticated: GithubUser,
        signup_attribution: UserSignupAttribution | None = None,
    ) -> tuple[User, bool]:
        # Check if we have an existing user with this GitHub account
        existing_user_by_id = await self.get_user_by_github_id(
            session, id=authenticated.id
        )
        if existing_user_by_id:
            user = await self.get_updated(
                session,
                github_user=authenticated,
                user=existing_user_by_id,
                tokens=tokens,
                client=client,
            )
            return (user, False)

        # Fetch user email
        github_email = await self.fetch_authenticated_user_primary_email(client=client)

        # Check if existing user with this email
        email, email_verified = github_email
        existing_user_by_email = await self.get_by_email(session, email)
        if existing_user_by_email:
            # Automatically link if email is verified
            if email_verified:
                user = await self.get_updated(
                    session,
                    github_user=authenticated,
                    user=existing_user_by_email,
                    tokens=tokens,
                    client=client,
                )
                return (user, False)

            else:
                # For security reasons, don't link if the email is not verified
                raise CannotLinkUnverifiedEmailError(email)

        # New user
        user = await self.create(
            session,
            github_user=authenticated,
            github_email=github_email,
            tokens=tokens,
            signup_attribution=signup_attribution,
        )
        return (user, True)

    async def link_existing_user(
        self, session: AsyncSession, *, user: User, tokens: OAuthAccessToken
    ) -> User:
        client = github.get_client(access_token=tokens.access_token)
        github_user = await self.fetch_authenticated_user(client=client)
        email, _ = await self.fetch_authenticated_user_primary_email(client=client)

        account_id = str(github_user.id)

        existing_oauth = await oauth_account_service.get_by_platform_and_username(
            session, OAuthPlatform.github, github_user.login
        )
        if existing_oauth is not None and existing_oauth.user_id != user.id:
            raise AccountLinkedToAnotherUserError()

        # Create or update OAuthAccount
        oauth_account = await oauth_account_service.get_by_platform_and_account_id(
            session, OAuthPlatform.github, account_id
        )
        if oauth_account is not None:
            if oauth_account.user_id != user.id:
                raise AccountLinkedToAnotherUserError()
        else:
            oauth_account = OAuthAccount(
                platform=OAuthPlatform.github,
                account_id=account_id,
                account_email=email,
            )
            user.oauth_accounts.append(oauth_account)

        oauth_account.access_token = tokens.access_token
        oauth_account.expires_at = tokens.expires_at
        oauth_account.refresh_token = tokens.refresh_token
        oauth_account.refresh_token_expires_at = tokens.refresh_token_expires_at
        oauth_account.account_email = email
        oauth_account.account_username = github_user.login
        session.add(oauth_account)

        # Update User profile
        user.avatar_url = github_user.avatar_url
        session.add(user)

        return user

    async def fetch_authenticated_user(
        self, *, client: GitHub[TokenAuthStrategy]
    ) -> GithubUser:
        response = await client.rest.users.async_get_authenticated()
        github.ensure_expected_response(response)
        return response.parsed_data

    async def fetch_authenticated_user_primary_email(
        self, *, client: GitHub[TokenAuthStrategy]
    ) -> GithubEmail:
        email_response = (
            await client.rest.users.async_list_emails_for_authenticated_user()
        )

        try:
            github.ensure_expected_response(email_response)
        except Exception as e:
            log.error("fetch_authenticated_user_primary_email.failed", err=e)
            raise NoPrimaryEmailError() from e

        emails = email_response.parsed_data

        for email in emails:
            if email.primary:
                return email.email, email.verified

        raise NoPrimaryEmailError()


github_user = GithubUserService(User)
