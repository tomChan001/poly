from fastapi import Request

from backend.app.container import ApplicationContainer


def get_container(request: Request) -> ApplicationContainer:
    return request.app.state.container

