def merge(left,right,key=lambda x:x):
    return iter(sorted([*left,*right],key=key))
