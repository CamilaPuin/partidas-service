class MatchNotFound(Exception):
    pass


class MatchUnavailable(Exception):
    pass


class OwnMatch(Exception):
    pass


class IncorrectSolution(Exception):
    pass


class MatchConflict(Exception):
    pass


class ServiceIntegrationError(Exception):
    pass


class CasesServiceError(ServiceIntegrationError):
    pass


class CaseNotFound(CasesServiceError):
    pass


class UserResultConflict(ServiceIntegrationError):
    pass
