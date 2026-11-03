from django.shortcuts import render


def board(request):
    return render(request, "testbed/board_page.html", context={"title": "board"})
