import dill
import sys
import filecmp
import math
import numpy
import pandas as pd
import os

from funcx.serialize.concretes import (pickle_base64,
                                       code_pickle, code_text_dill,
                                       code_text_inspect)

if not os.path.exists('serials'):
    os.makedirs('serials')
    os.makedirs('serials/3.6')
    os.makedirs('serials/3.7')
    os.makedirs('serials/3.8')

def func0(x, y={'a': 3}):
    return math.floor(math.log(x)) * y['a']

def func1(x):
    return numpy.linalg.det(x)

def func2(self):
    return self.product(0).iloc[0]

def func3(arr, low, high, x):
    if high >= low:
        mid = (high + low) // 2
        if arr[mid] == x:
            return mid
        elif arr[mid] > x:
            return func3(arr, low, mid - 1, x)
        else:
            return func3(arr, mid + 1, high, x)
    else:
        return -1

def func4(word_list):
    return sorted(word_list)

def func5(word):
    return word[::-2]

def func6(x):
    if (x > 10):
        raise Exception('input is too large')

test_functions = [func0, func1, func2, func3, func4, func5, func6]

arr1 = numpy.array([[1, 2], [3, 4]])
data = {'B':[15, 21, 59, 92, 35],
        'A':['A', 'B', 'C', 'D', 'E']}
df = pd.DataFrame(data)
arr2 = [-1,0,3,5,9,12]
word_list = ["Hello", "this", "Is", "an", "Example"]
some_string = "asdfghjkl;"

func_args = [10042, arr1, df, (arr2, 0, 6, 9), word_list, some_string]

s02 = code_pickle()
s03 = code_text_dill()
s04 = code_text_inspect()

serializers = [s02, s03, s04]

expected_outputs = []

for i, func in enumerate(test_functions):
    if i == len(test_functions) - 1:
        expected_outputs.append(0)
    elif type(func_args[i]) is tuple:
        expected_outputs.append(func(*func_args[i]))
    else:
        expected_outputs.append(func(func_args[i]))

# write function serials to files
if __name__ == "__main__":
    version = str(sys.version_info[0]) + "." + str(sys.version_info[1])
    print("Current version:", version)
    for i in range(len(serializers)):
        for j in range(len(test_functions)):
            payload = serializers[i].serialize(test_functions[j])
            f_name = "serials/" + version + "/func" + \
                      str(j) + "_0" + str(i+2) + ".txt"
            fp = open(f_name, "w")
            fp.write(payload)
            fp.close()
    print("Wrote serials to serials/" + version)
