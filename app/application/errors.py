class MatchNotFound(Exception):
    pass


class MatchUnavailable(Exception):
    pass


class OwnMatch(Exception):
    pass


class IncorrectSolution(Exception):
    pass


class ServiceIntegrationError(Exception):
    pass


class CasesServiceError(ServiceIntegrationError):
    pass
