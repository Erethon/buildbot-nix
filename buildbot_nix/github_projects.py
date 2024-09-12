import json
import os
import signal
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from itertools import starmap
from pathlib import Path
from typing import Any, cast
from datetime import datetime, timezone

from zope.interface import implementer
from buildbot.config.builder import BuilderConfig
from buildbot.plugins import util
from buildbot.process.buildstep import BuildStep
from buildbot.process.build import Build
from buildbot.process.properties import Interpolate, Properties, WithProperties
from buildbot.reporters.base import ReporterBase
from buildbot.reporters.utils import getDetailsForBuild
from buildbot.reporters.github import GitHubStatusPush
from buildbot.reporters.generators.utils import BuildStatusGeneratorMixin
from buildbot.reporters.generators.build import BuildStartEndStatusGenerator
from buildbot.reporters.generators.buildrequest import BuildRequestGenerator
from buildbot.secrets.providers.base import SecretProviderBase
from buildbot.www.auth import AuthBase
from buildbot.www.avatar import AvatarBase, AvatarGitHub
from buildbot.www.oauth2 import GitHubAuth
from buildbot.master import BuildMaster
from pydantic import BaseModel, ConfigDict, Field
from twisted.internet import defer
from twisted.logger import Logger
from twisted.python import log
from buildbot.interfaces import IReportGenerator, IRenderable
from buildbot.reporters.message import MessageFormatterRenderable
from buildbot.plugins import util

from .common import (
    ThreadDeferredBuildStep,
    atomic_write_file,
    filter_for_combined_builds,
    filter_repos_by_topic,
    http_request,
    model_dump_project_cache,
    model_validate_project_cache,
    paginated_github_request,
    slugify_project_name,
)
from .github.installation_token import InstallationToken
from .github.jwt_token import JWTToken
from .github.legacy_token import (
    LegacyToken,
)
from .github.repo_token import (
    RepoToken,
)
from .models import (
    GitHubAppConfig,
    GitHubConfig,
    GitHubLegacyConfig,
)
from .projects import GitBackend, GitProject

tlog = Logger()


class RepoOwnerData(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    login: str
    ttype: str = Field(alias="type")


class RepoData(BaseModel):
    name: str
    owner: RepoOwnerData
    full_name: str
    html_url: str
    default_branch: str
    topics: list[str]
    installation_id: int | None


def get_installations(jwt_token: JWTToken) -> list[int]:
    installations = paginated_github_request(
        "https://api.github.com/app/installations?per_page=100", jwt_token.get()
    )

    return [installation["id"] for installation in installations]


class CreateGitHubInstallationHooks(ThreadDeferredBuildStep):
    name = "create_github_installation_hooks"

    jwt_token: JWTToken
    project_cache_file: Path
    installation_token_map_name: Path
    webhook_secret: str
    webhook_url: str
    topic: str | None

    def __init__(
        self,
        jwt_token: JWTToken,
        project_cache_file: Path,
        installation_token_map_name: Path,
        webhook_secret: str,
        webhook_url: str,
        topic: str | None,
        **kwargs: Any,
    ) -> None:
        self.jwt_token = jwt_token
        self.project_cache_file = project_cache_file
        self.installation_token_map_name = installation_token_map_name
        self.webhook_secret = webhook_secret
        self.webhook_url = webhook_url
        self.topic = topic
        super().__init__(**kwargs)

    def run_deferred(self) -> None:
        repos = model_validate_project_cache(RepoData, self.project_cache_file)
        installation_token_map: dict[int, InstallationToken] = dict(
            starmap(
                lambda k, v: (
                    int(k),
                    InstallationToken.from_json(
                        self.jwt_token, int(k), self.installation_token_map_name, v
                    ),
                ),
                json.loads(self.installation_token_map_name.read_text()).items(),
            )
        )

        for repo in repos:
            if repo.installation_id is None:
                continue

            create_project_hook(
                installation_token_map[repo.installation_id],
                self.webhook_secret,
                repo.owner.login,
                repo.name,
                self.webhook_url,
            )

    def run_post(self) -> Any:
        # reload the buildbot config
        os.kill(os.getpid(), signal.SIGHUP)
        return util.SUCCESS


class ReloadGithubInstallations(ThreadDeferredBuildStep):
    name = "reload_github_projects"

    jwt_token: JWTToken
    project_cache_file: Path
    installation_token_map_name: Path
    project_id_map_name: Path
    topic: str | None

    def __init__(
        self,
        jwt_token: JWTToken,
        project_cache_file: Path,
        installation_token_map_name: Path,
        project_id_map_name: Path,
        topic: str | None,
        **kwargs: Any,
    ) -> None:
        self.jwt_token = jwt_token
        self.installation_token_map_name = installation_token_map_name
        self.project_id_map_name = project_id_map_name
        self.project_cache_file = project_cache_file
        self.topic = topic
        super().__init__(**kwargs)

    def run_deferred(self) -> None:
        installation_token_map = GithubBackend.create_missing_installations(
            self.jwt_token,
            self.installation_token_map_name,
            GithubBackend.load_installations(
                self.jwt_token,
                self.installation_token_map_name,
            ),
            get_installations(self.jwt_token),
        )

        repos: list[RepoData] = []
        project_id_map: dict[str, int] = {}

        repos = []

        for k, v in installation_token_map.items():
            new_repos = filter_repos_by_topic(
                self.topic,
                refresh_projects(
                    v.get(),
                    self.project_cache_file,
                    clear=True,
                    api_endpoint="/installation/repositories",
                    subkey="repositories",
                    require_admin=False,
                ),
                lambda repo: repo.topics,
            )

            for repo in new_repos:
                repo.installation_id = k

                project_id_map[repo.full_name] = k

            repos.extend(new_repos)

        atomic_write_file(self.project_cache_file, model_dump_project_cache(repos))
        atomic_write_file(self.project_id_map_name, json.dumps(project_id_map))

        tlog.info(
            f"Fetched {len(repos)} repositories from {len(installation_token_map.items())} installation tokens."
        )

    def run_post(self) -> Any:
        return util.SUCCESS


class CreateGitHubProjectHooks(ThreadDeferredBuildStep):
    name = "create_github_project_hooks"

    token: RepoToken
    project_cache_file: Path
    webhook_secret: str
    webhook_url: str
    topic: str | None

    def __init__(
        self,
        token: RepoToken,
        project_cache_file: Path,
        webhook_secret: str,
        webhook_url: str,
        topic: str | None,
        **kwargs: Any,
    ) -> None:
        self.token = token
        self.project_cache_file = project_cache_file
        self.webhook_secret = webhook_secret
        self.webhook_url = webhook_url
        self.topic = topic
        super().__init__(**kwargs)

    def run_deferred(self) -> None:
        repos = model_validate_project_cache(RepoData, self.project_cache_file)

        for repo in repos:
            create_project_hook(
                self.token,
                self.webhook_secret,
                repo.owner.login,
                repo.name,
                self.webhook_url,
            )

    def run_post(self) -> Any:
        # reload the buildbot config
        os.kill(os.getpid(), signal.SIGHUP)
        return util.SUCCESS


class ReloadGithubProjects(ThreadDeferredBuildStep):
    name = "reload_github_projects"

    token: RepoToken
    project_cache_file: Path
    topic: str | None

    def __init__(
        self,
        token: RepoToken,
        project_cache_file: Path,
        topic: str | None,
        **kwargs: Any,
    ) -> None:
        self.token = token
        self.project_cache_file = project_cache_file
        self.topic = topic
        super().__init__(**kwargs)

    def run_deferred(self) -> None:
        repos: list[RepoData] = filter_repos_by_topic(
            self.topic,
            refresh_projects(self.token.get(), self.project_cache_file),
            lambda repo: repo.topics,
        )

        atomic_write_file(self.project_cache_file, model_dump_project_cache(repos))

    def run_post(self) -> Any:
        return util.SUCCESS


class GithubAuthBackend(ABC):
    @abstractmethod
    def get_general_token(self) -> RepoToken:
        pass

    @abstractmethod
    def get_repo_token(self, repo_full_name: str) -> RepoToken:
        pass

    @abstractmethod
    def create_secret_providers(self) -> list[SecretProviderBase]:
        pass

    @abstractmethod
    def create_reporter(self) -> ReporterBase:
        pass

    @abstractmethod
    def create_reload_builder_steps(
        self,
        project_cache_file: Path,
        webhook_secret: str,
        webhook_url: str,
        topic: str | None,
    ) -> list[BuildStep]:
        pass

@implementer(IReportGenerator)
class BuildNixEvalStatusGenerator(BuildStatusGeneratorMixin):
    wanted_event_keys = [
        ('builds', None, 'started-nix-eval'),
        ('builds', None, 'finished-nix-eval'),
        ('builds', None, 'started-nix-build'),
        ('builds', None, 'finished-nix-build'),
    ]

    compare_attrs = ['start_formatter', 'end_formatter']

    start_formatter: IRenderable
    end_formatter: IRenderable

    def __init__(
        self,
        tags: None | list[str] = None,
        builders: None | list[str] = None,
        schedulers: None | list[str] = None,
        branches: None | list[str] = None,
        add_logs: bool | list[str] = False,
        add_patch: bool = False,
        start_formatter: None | IRenderable = None,
        end_formatter: None | IRenderable = None,
    ):
        super().__init__('all', tags, builders, schedulers, branches, None, add_logs, add_patch)
        self.start_formatter = start_formatter
        if self.start_formatter is None:
            self.start_formatter = MessageFormatterRenderable('Build started.')
        self.end_formatter = end_formatter
        if self.end_formatter is None:
            self.end_formatter = MessageFormatterRenderable('Build done.')

    @defer.inlineCallbacks
    def generate(self, master: BuildMaster, reporter: ReporterBase, key: tuple[str, None | Any, str], build: Build) -> defer.Generator[Any, object, Any]:
        _, _, event = key
        is_new = event == 'new'

        formatter = self.start_formatter if is_new else self.end_formatter

        yield getDetailsForBuild(
            master,
            build,
            want_properties=formatter.want_properties,
            want_steps=formatter.want_steps,
            want_logs=formatter.want_logs,
            want_logs_content=formatter.want_logs_content,
        )

        if not self.is_message_needed_by_props(build):
            return None

        report = yield self.build_message(formatter, master, reporter, build)
        reportT: dict[str, Any] = cast(dict[str, Any], report)

        if event == "started-nix-eval" or event == "finished-nix-eval":
            reportT["builds"][0]["properties"]["status_name"] = ("nix-eval", "generator")
        if (event == "started-nix-build" or event == "finished-nix-build") and reportT["builds"][0]["properties"]["status_name"][0] == "nix-eval":
            reportT["builds"][0]["properties"]["status_name"] = ("nix-build", "generator")
        if event == "finished-nix-eval" or event == "finished-nix-build":
            reportT["builds"][0]["complete"] = True
            reportT["builds"][0]["complete_at"] = datetime.now(tz=timezone.utc)

        tlog.info("{event}: {build}", event = event, build = reportT["builds"][0])

        return reportT

class ModifyingGitHubStatusPush(GitHubStatusPush):
    def checkConfig(
        self,
        modifyingFilter: Callable[[Any], Any | None] = lambda x: x,  # noqa: N803
        generators: None | list[IReportGenerator] = None,
        **kwargs: Any,
    ) -> Any:
        self.modifyingFilter = modifyingFilter

        generators = [
            # BuildRequestGenerator(),
            # BuildStartEndStatusGenerator(),
            BuildNixEvalStatusGenerator(),
        ]

        return super().checkConfig(**kwargs, generators = generators)

    def reconfigService(
        self,
        modifyingFilter: Callable[[Any], Any | None] = lambda x: x,  # noqa: N803
        generators: None | list[IReportGenerator] = None,
        **kwargs: Any,
    ) -> Any:
        self.modifyingFilter = modifyingFilter

        generators = [
            # BuildRequestGenerator(),
            # BuildStartEndStatusGenerator(),
            BuildNixEvalStatusGenerator(),
        ]

        return super().reconfigService(**kwargs, generators = generators)

    @defer.inlineCallbacks
    def sendMessage(self, reports: Any) -> Any:
        reports = self.modifyingFilter(reports)
        if reports is None:
            return

        result = yield super().sendMessage(reports)
        return result


class GithubLegacyAuthBackend(GithubAuthBackend):
    auth_type: GitHubLegacyConfig

    token: LegacyToken

    def __init__(self, auth_type: GitHubLegacyConfig) -> None:
        self.auth_type = auth_type
        self.token = LegacyToken(auth_type.token)

    def get_general_token(self) -> RepoToken:
        return self.token

    def get_repo_token(self, repo_full_name: str) -> RepoToken:
        return self.token

    def create_secret_providers(self) -> list[SecretProviderBase]:
        return [GitHubLegacySecretService(self.token)]

    def create_reporter(self) -> ReporterBase:
        return ModifyingGitHubStatusPush(
            token=self.token.get(),
            # Since we dynamically create build steps,
            # we use `virtual_builder_name` in the webinterface
            # so that we distinguish what has beeing build
            context=Interpolate("buildbot/%(prop:status_name)s"),
            modifyingFilter=filter_for_combined_builds,
        )

    def create_reload_builder_steps(
        self,
        project_cache_file: Path,
        webhook_secret: str,
        webhook_url: str,
        topic: str | None,
    ) -> list[BuildStep]:
        return [
            ReloadGithubProjects(
                token=self.token,
                project_cache_file=project_cache_file,
                topic=topic,
            ),
            CreateGitHubProjectHooks(
                token=self.token,
                project_cache_file=project_cache_file,
                webhook_secret=webhook_secret,
                webhook_url=webhook_url,
                topic=topic,
            ),
        ]


class GitHubLegacySecretService(SecretProviderBase):
    name = "GitHubLegacySecretService"
    token: LegacyToken

    def reconfigService(self, token: LegacyToken) -> None:
        self.token = token

    def get(self, entry: str) -> str | None:
        """
        get the value from the file identified by 'entry'
        """
        if entry.startswith("github-token"):
            return self.token.get()
        return None


class GithubAppAuthBackend(GithubAuthBackend):
    auth_type: GitHubAppConfig

    jwt_token: JWTToken
    installation_tokens: dict[int, InstallationToken]
    project_id_map: dict[str, int]

    def __init__(self, auth_type: GitHubAppConfig) -> None:
        self.auth_type = auth_type
        self.jwt_token = JWTToken(self.auth_type.id, self.auth_type.secret_key_file)
        self.installation_tokens = GithubBackend.load_installations(
            self.jwt_token,
            self.auth_type.installation_token_map_file,
        )
        if self.auth_type.project_id_map_file.exists():
            self.project_id_map = json.loads(
                self.auth_type.project_id_map_file.read_text()
            )
        else:
            tlog.info(
                "~project-id-map~ is not present, GitHub project reload will follow."
            )
            self.project_id_map = {}

    def get_general_token(self) -> RepoToken:
        return self.jwt_token

    def get_repo_token(self, repo_full_name: str) -> RepoToken:
        installation_id = self.project_id_map[repo_full_name]
        return self.installation_tokens[installation_id]

    def create_secret_providers(self) -> list[SecretProviderBase]:
        return [GitHubAppSecretService(self.installation_tokens, self.jwt_token)]

    def create_reporter(self) -> ReporterBase:
        def get_github_token(props: Properties) -> str:
            return self.installation_tokens[
                self.project_id_map[props["projectname"]]
            ].get()

        return ModifyingGitHubStatusPush(
            token=WithProperties("%(github_token)s", github_token=get_github_token),
            # Since we dynamically create build steps,
            # we use `virtual_builder_name` in the webinterface
            # so that we distinguish what has beeing build
            context=Interpolate("buildbot/%(prop:status_name)s"),
            modifyingFilter=filter_for_combined_builds,
        )

    def create_reload_builder_steps(
        self,
        project_cache_file: Path,
        webhook_secret: str,
        webhook_url: str,
        topic: str | None,
    ) -> list[BuildStep]:
        return [
            ReloadGithubInstallations(
                self.jwt_token,
                project_cache_file,
                self.auth_type.installation_token_map_file,
                self.auth_type.project_id_map_file,
                topic,
            ),
            CreateGitHubInstallationHooks(
                self.jwt_token,
                project_cache_file,
                self.auth_type.installation_token_map_file,
                webhook_secret=webhook_secret,
                webhook_url=webhook_url,
                topic=topic,
            ),
        ]


class GitHubAppSecretService(SecretProviderBase):
    name = "GitHubAppSecretService"
    installation_tokens: dict[int, InstallationToken]
    jwt_token: JWTToken

    def reconfigService(
        self, installation_tokens: dict[int, InstallationToken], jwt_token: JWTToken
    ) -> None:
        self.installation_tokens = installation_tokens
        self.jwt_token = jwt_token

    def get(self, entry: str) -> str | None:
        """
        get the value from the file identified by 'entry'
        """
        if entry.startswith("github-token-"):
            return self.installation_tokens[
                int(entry.removeprefix("github-token-"))
            ].get()
        if entry == "github-jwt-token":
            return self.jwt_token.get()
        return None


@dataclass
class GithubBackend(GitBackend):
    config: GitHubConfig
    webhook_secret: str
    webhook_url: str

    auth_backend: GithubAuthBackend

    def __init__(self, config: GitHubConfig, webhook_url: str) -> None:
        self.config = config
        self.webhook_secret = self.config.webhook_secret
        self.webhook_url = webhook_url

        if isinstance(self.config.auth_type, GitHubLegacyConfig):
            self.auth_backend = GithubLegacyAuthBackend(self.config.auth_type)
        elif isinstance(self.config.auth_type, GitHubAppConfig):
            self.auth_backend = GithubAppAuthBackend(self.config.auth_type)

    @staticmethod
    def load_installations(
        jwt_token: JWTToken, installations_token_map_name: Path
    ) -> dict[int, InstallationToken]:
        initial_installations_map: dict[str, Any]
        if installations_token_map_name.exists():
            initial_installations_map = json.loads(
                installations_token_map_name.read_text()
            )
        else:
            initial_installations_map = {}

        installations_map: dict[int, InstallationToken] = {}

        for iid, installation in initial_installations_map.items():
            installations_map[int(iid)] = InstallationToken.from_json(
                jwt_token, int(iid), installations_token_map_name, installation
            )

        return installations_map

    @staticmethod
    def create_missing_installations(
        jwt_token: JWTToken,
        installations_token_map_name: Path,
        installations_map: dict[int, InstallationToken],
        installations: list[int],
    ) -> dict[int, InstallationToken]:
        for installation in set(installations) - installations_map.keys():
            installations_map[installation] = InstallationToken.new(
                jwt_token,
                installation,
                installations_token_map_name,
            )

        return installations_map

    def create_reload_builder(self, worker_names: list[str]) -> BuilderConfig:
        """Updates the flake an opens a PR for it."""
        factory = util.BuildFactory()
        steps = self.auth_backend.create_reload_builder_steps(
            self.config.project_cache_file,
            self.webhook_secret,
            self.webhook_url,
            self.config.topic,
        )
        for step in steps:
            factory.addStep(step)

        return util.BuilderConfig(
            name=self.reload_builder_name,
            workernames=worker_names,
            factory=factory,
        )

    def create_reporter(self) -> ReporterBase:
        return self.auth_backend.create_reporter()

    def create_change_hook(self) -> dict[str, Any]:
        def get_github_token(props: Properties) -> str:
            return self.auth_backend.get_repo_token(
                props.getProperty("full_name")
            ).get()

        return {
            "secret": self.webhook_secret,
            "strict": True,
            "token": WithProperties("%(github_token)s", github_token=get_github_token),
            "github_property_whitelist": ["github.base.sha", "github.head.sha"],
        }

    def create_avatar_method(self) -> AvatarBase | None:
        return AvatarGitHub()

    def create_auth(self) -> AuthBase:
        assert self.config.oauth_id is not None, "GitHub OAuth ID is required"
        return GitHubAuth(
            self.config.oauth_id,
            self.config.oauth_secret,
            apiVersion=4,
        )

    def create_secret_providers(self) -> list[SecretProviderBase]:
        return self.auth_backend.create_secret_providers()

    def load_projects(self) -> list["GitProject"]:
        if not self.config.project_cache_file.exists():
            return []

        repos: list[RepoData] = filter_repos_by_topic(
            self.config.topic,
            sorted(
                model_validate_project_cache(RepoData, self.config.project_cache_file),
                key=lambda repo: repo.full_name,
            ),
            lambda repo: repo.topics,
        )

        if isinstance(self.auth_backend, GithubAppAuthBackend):
            dropped_repos = list(
                filter(lambda repo: repo.installation_id is None, repos)
            )
            if dropped_repos:
                tlog.info(
                    "Dropped projects follow, refresh will follow after initialisation:"
                )
                for dropped_repo in dropped_repos:
                    tlog.info(f"\tDropping repo {dropped_repo.full_name}")
            repos = list(filter(lambda repo: repo.installation_id is not None, repos))

        repo_names: list[str] = [repo.owner.login + "/" + repo.name for repo in repos]

        tlog.info(
            f"Loading {len(repos)} cached repositories: [{', '.join(repo_names)}]"
        )
        return [
            GithubProject(
                self.auth_backend.get_repo_token(repo.full_name),
                self.config,
                self.webhook_secret,
                RepoData.model_validate(repo),
            )
            for repo in repos
        ]

    def are_projects_cached(self) -> bool:
        if not self.config.project_cache_file.exists():
            return False

        if (
            isinstance(self.config.auth_type, GitHubAppConfig)
            and not self.config.auth_type.project_id_map_file.exists()
        ):
            return False

        all_have_installation_id = True
        for project in model_validate_project_cache(
            RepoData, self.config.project_cache_file
        ):
            if project.installation_id is not None:
                all_have_installation_id = False
                break

        return all_have_installation_id

    @property
    def type(self) -> str:
        return "github"

    @property
    def pretty_type(self) -> str:
        return "GitHub"

    @property
    def reload_builder_name(self) -> str:
        return "reload-github-projects"

    @property
    def change_hook_name(self) -> str:
        return "github"


def create_project_hook(
    token: RepoToken, webhook_secret: str, owner: str, repo: str, webhook_url: str
) -> None:
    hooks = paginated_github_request(
        f"https://api.github.com/repos/{owner}/{repo}/hooks?per_page=100",
        token.get(),
    )
    config = dict(
        url=webhook_url + "change_hook/github",
        content_type="json",
        insecure_ssl="0",
        secret=webhook_secret,
    )
    data = dict(name="web", active=True, events=["push", "pull_request"], config=config)
    headers = {
        "Authorization": f"Bearer {token.get()}",
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    for hook in hooks:
        if hook["config"]["url"] == webhook_url + "change_hook/github":
            log.msg(f"hook for {owner}/{repo} already exists")
            return

    http_request(
        f"https://api.github.com/repos/{owner}/{repo}/hooks",
        method="POST",
        headers=headers,
        data=data,
    )


class GithubProject(GitProject):
    config: GitHubConfig
    webhook_secret: str
    data: RepoData
    token: RepoToken

    def __init__(
        self,
        token: RepoToken,
        config: GitHubConfig,
        webhook_secret: str,
        data: RepoData,
    ) -> None:
        self.token = token
        self.config = config
        self.webhook_secret = webhook_secret
        self.data = data

    def get_project_url(self) -> str:
        return f"https://git:{self.token.get_as_secret()}s@github.com/{self.name}"

    @property
    def pretty_type(self) -> str:
        return "GitHub"

    @property
    def type(self) -> str:
        return "github"

    @property
    def repo(self) -> str:
        return self.data.name

    @property
    def owner(self) -> str:
        return self.data.owner.login

    @property
    def name(self) -> str:
        return self.data.full_name

    @property
    def url(self) -> str:
        return self.data.html_url

    @property
    def project_id(self) -> str:
        return slugify_project_name(self.data.full_name)

    @property
    def default_branch(self) -> str:
        return self.data.default_branch

    @property
    def topics(self) -> list[str]:
        return self.data.topics

    @property
    def belongs_to_org(self) -> bool:
        return self.data.owner.ttype == "Organization"


def refresh_projects(
    github_token: str,
    repo_cache_file: Path,
    repos: list[Any] | None = None,
    clear: bool = True,
    api_endpoint: str = "/user/repos",
    subkey: None | str = None,
    require_admin: bool = True,
) -> list[RepoData]:
    if repos is None:
        repos = []

    for repo in paginated_github_request(
        f"https://api.github.com{api_endpoint}?per_page=100",
        github_token,
        subkey=subkey,
    ):
        # TODO actually check for this properly
        if not repo["permissions"]["admin"] and require_admin:
            name = repo["full_name"]
            log.msg(
                f"skipping {name} because we do not have admin privileges, needed for hook management",
            )
        else:
            repo["installation_id"] = None
            repos.append(RepoData.model_validate(repo))

    return repos
