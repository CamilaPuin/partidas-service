from app.application import errors
from app.core import service_clients
from app.services import matches


def test_shared_error_classes_keep_legacy_import_identity():
    assert matches.MatchNotFound is errors.MatchNotFound
    assert matches.MatchUnavailable is errors.MatchUnavailable
    assert matches.OwnMatch is errors.OwnMatch
    assert matches.IncorrectSolution is errors.IncorrectSolution
    assert matches.MatchConflict is errors.MatchConflict
    assert service_clients.CasesServiceError is errors.CasesServiceError
    assert service_clients.ServiceIntegrationError is errors.ServiceIntegrationError
    assert service_clients.CaseNotFound is errors.CaseNotFound
    assert service_clients.UserResultConflict is errors.UserResultConflict
