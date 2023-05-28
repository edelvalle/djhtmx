(function() {
    function _runHxAfterSwap(element) {
        const code = element.getAttribute('hx-after-swap');
        if (code) (() => eval(code)).bind(element)();
    }


    document.body.addEventListener('htmx:afterSwap', (event) => {
        _runHxAfterSwap(event.detail.target);
        event.detail.target
            .querySelectorAll('[hx-after-swap]')
            .forEach(_runHxAfterSwap);
    });


    document.body.addEventListener('htmx:configRequest', (event) => {
        const csrf_header = document
            .querySelector('meta[name=django-htmx-csrf-header-name]')
            .getAttribute('content');
        const csrf_token = document
            .querySelector('meta[name=django-htmx-csrf-token]')
            .getAttribute('content');
        event.detail.headers[csrf_header] = csrf_token;
    });

    document.addEventListener('hxSendEvent', (event) => {
        event.detail.value.map(({ event, target }) => {
            document.querySelector(target).dispatchEvent(new Event(event));
        });
    });


    document.addEventListener('hxFocus', (event) => {
        event.detail.value.map((selector) => {
            document.querySelector(selector).focus();
        });
    });
})();
