from flask import render_template as flask_render_template, request

def render_template(template_name_or_list, **context):
    ua_string = request.headers.get('User-Agent', '').lower()
    is_mobile = any(kw in ua_string for kw in ['mobile', 'android', 'iphone', 'ipad', 'ipod', 'windows phone'])
    
    if is_mobile:
        return flask_render_template(f"mobile/mobile_{template_name_or_list}", **context)
    else:
        return flask_render_template(f"pc/pc_{template_name_or_list}", **context)