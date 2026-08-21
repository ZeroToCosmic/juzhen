"""Campaign-local types and persistence; API/execution integrations are separate."""

from .domain import AssignmentStatus, CampaignMode, CampaignStatus, transition_assignment, transition_campaign
__all__ = ["AssignmentStatus", "CampaignMode", "CampaignStatus", "CampaignStore", "transition_assignment", "transition_campaign"]


def __getattr__(name: str):
    """Keep queue/worker modules importable when optional persistence deps are absent."""
    if name == "CampaignStore":
        from .store import CampaignStore

        return CampaignStore
    raise AttributeError(name)
